"""Archive frames to a second location and free local space only from verified copies.

The rule everything here serves: a local file may be deleted only if a copy of it
has been written to the archive, read back, and found byte-identical. That is what
makes "delete when the disk is nearly full" safe to leave running unattended - the
worst case when the archive is unreachable is that nothing is deleted and the
capture pauses on its own disk guard, never that the last copy of a night is lost.

    copy      every settled file under originals/, captures/ and derived/ is written
              to `<archive>/<same relative path>.part`, fsynced, read back and hashed,
              then renamed into place. A row in `archived_files` records the hash.
    delete    when free space drops below the trigger (or a file is older than the
              configured age) verified captures and originals are removed, oldest day
              first, until free space is back above the target. Today's frames and any
              day a processing session is still open on are never touched.

Derived products are copied but never deleted: database rows point at them.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from app import astro
from app.config import get_settings
from app.db.database import SessionLocal
from app.models.archived_file import ArchivedFile
from app.repositories.processing_repository import ProcessingSessionRepository

logger = structlog.get_logger()

# A frame is rewritten by the overlay renderer for a few seconds after upload, and
# the node may still be writing it. Copying only files that have been quiet for
# this long means a hash taken mid-write cannot happen.
SETTLE_SECONDS = 120
CHUNK_BYTES = 1024 * 1024
CYCLE_BUDGET_SECONDS = 600
COMMIT_EVERY = 100
# A dead share fails every file the same way; stop rather than log thousands of lines.
MAX_CONSECUTIVE_FAILURES = 5
DELETABLE_SOURCES = ("captures", "originals")
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_cycle_lock = threading.Lock()
_state: dict = {"running": False, "last_run_at": None, "last_result": None, "last_error": None}


class ArchiveError(Exception):
    """A copy that did not verify. The partial file is already gone when this is raised."""


@dataclass
class CycleResult:
    copied_files: int = 0
    copied_bytes: int = 0
    failed_files: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    stopped_early: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "copied_files": self.copied_files,
            "copied_bytes": self.copied_bytes,
            "failed_files": self.failed_files,
            "deleted_files": self.deleted_files,
            "deleted_bytes": self.deleted_bytes,
            "stopped_early": self.stopped_early,
            "notes": self.notes,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def configured_sources() -> list[str]:
    allowed = {"originals", "captures", "derived"}
    wanted = [name.strip() for name in get_settings().archive_sources.split(",")]

    return [name for name in wanted if name in allowed]


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


def archive_problem() -> str | None:
    """Why the archive cannot be used right now, or None when it can."""
    settings = get_settings()
    archive_dir = settings.archive_dir

    if archive_dir is None:
        return "archive_dir is not set"

    if not archive_dir.is_dir():
        return f"{archive_dir} does not exist - is the share mounted?"

    if not os.access(archive_dir, os.W_OK):
        return f"{archive_dir} is not writable"

    if settings.archive_require_mount:
        try:
            same_disk = archive_dir.stat().st_dev == settings.data_dir.stat().st_dev
        except OSError as error:
            return f"cannot stat the archive: {error}"

        if same_disk:
            return (
                f"{archive_dir} is on the same filesystem as the data directory - "
                "refusing to use it (mount the share, or set SKYHUB_SERVER_ARCHIVE_REQUIRE_MOUNT=false)"
            )

    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)

    return digest.hexdigest()


def copy_verified(source: Path, destination: Path) -> tuple[int, str]:
    """Copy, fsync, read back and hash-compare. Returns (size, sha256).

    The destination only ever appears under its real name once it has verified, so
    an interrupted run leaves a `.part` file and never a truncated "archived" one.
    """
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    size = 0

    try:
        with source.open("rb") as reader, partial.open("wb") as writer:
            while chunk := reader.read(CHUNK_BYTES):
                digest.update(chunk)
                writer.write(chunk)
                size += len(chunk)

            writer.flush()
            os.fsync(writer.fileno())

        after = source.stat()

        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ArchiveError(f"{source.name} changed while it was being copied")

        if partial.stat().st_size != size or sha256_of(partial) != digest.hexdigest():
            raise ArchiveError(f"{source.name} did not verify after copying")

        os.replace(partial, destination)

    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    return size, digest.hexdigest()


def relative(path: Path) -> str:
    return path.relative_to(get_settings().data_dir).as_posix()


def walk_oldest_first(root: Path):
    """Yield (directory, files) with directories in name order, so oldest dates go first."""
    for current, directories, files in os.walk(root):
        directories.sort()

        if files:
            yield Path(current), sorted(files)


# ---------- copy ----------


def copy_phase(result: CycleResult, deadline: float) -> None:
    settings = get_settings()
    archive_dir = settings.archive_dir
    settle_before = time.time() - SETTLE_SECONDS
    consecutive_failures = 0

    with SessionLocal() as db:
        for source_name in configured_sources():
            root = settings.data_dir / source_name

            if not root.is_dir():
                continue

            for directory, files in walk_oldest_first(root):
                prefix = relative(directory)
                known = {
                    row.rel_path: row.size_bytes
                    for row in db.query(ArchivedFile.rel_path, ArchivedFile.size_bytes).filter(
                        ArchivedFile.rel_path.like(f"{prefix}/%")
                    )
                }
                pending = 0

                for name in files:
                    if time.monotonic() > deadline:
                        result.stopped_early = True
                        db.commit()
                        return

                    if name.endswith(".part"):
                        continue

                    path = directory / name
                    rel = f"{prefix}/{name}"

                    try:
                        stat = path.stat()

                        if stat.st_mtime > settle_before:
                            continue

                        if known.get(rel) == stat.st_size:
                            continue

                        size, digest = copy_verified(path, archive_dir / rel)

                    except (OSError, ArchiveError) as error:
                        result.failed_files += 1
                        consecutive_failures += 1
                        logger.warning("archive.copy.failed", path=rel, error=str(error))

                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            result.stopped_early = True
                            result.notes.append("stopped after repeated copy failures")
                            db.commit()
                            return

                        continue

                    consecutive_failures = 0
                    db.merge(
                        ArchivedFile(rel_path=rel, size_bytes=size, sha256=digest, archived_at=utc_now())
                    )
                    result.copied_files += 1
                    result.copied_bytes += size
                    pending += 1

                    if pending >= COMMIT_EVERY:
                        db.commit()
                        pending = 0

                db.commit()


# ---------- delete ----------


def protected_dates() -> set[tuple[str, str]]:
    """(node_id, archive_date) pairs a processing session is still reading from."""
    with SessionLocal() as db:
        return {
            (record.node_id, record.archive_date)
            for record in ProcessingSessionRepository(db).list_open()
        }


def verified_on_archive(db, path: Path) -> int | None:
    """Size of the local file when a verified archive copy of it exists, else None."""
    settings = get_settings()

    try:
        local_size = path.stat().st_size
        rel = relative(path)
        row = db.get(ArchivedFile, rel)

        if row is None or row.size_bytes != local_size:
            return None

        if (settings.archive_dir / rel).stat().st_size != local_size:
            return None

    except OSError:
        return None

    return local_size


def delete_phase(result: CycleResult) -> None:
    settings = get_settings()
    trigger = settings.archive_free_trigger_gb
    target = settings.archive_free_target_gb
    freeing = free_gb(settings.data_dir) < trigger
    cutoff = None

    if settings.archive_delete_after_days > 0:
        cutoff = (utc_now() - timedelta(days=settings.archive_delete_after_days)).date().isoformat()

    if not freeing and cutoff is None:
        return

    if freeing:
        result.notes.append(f"free space below {trigger:g} GB, freeing to {target:g} GB")

    today = astro.archive_period(utc_now())[0]
    busy = protected_dates()

    with SessionLocal() as db:
        # Oldest first, so the first day that is neither too old nor needed for space
        # means every later day is not either.
        for date_name, directory, node_id in oldest_day_directories():
            if date_name >= today or (node_id, date_name) in busy:
                continue

            too_old = cutoff is not None and date_name < cutoff

            if freeing and free_gb(settings.data_dir) >= target:
                freeing = False

            if not too_old and not freeing:
                break

            delete_directory(db, directory, result, stop_at_target=not too_old)
            prune_empty(directory)


def delete_directory(db, directory: Path, result: CycleResult, stop_at_target: bool) -> None:
    """Remove every verified file under a day directory, optionally stopping once there is room."""
    settings = get_settings()

    for current, _directories, files in os.walk(directory):
        for name in sorted(files):
            path = Path(current) / name
            size = verified_on_archive(db, path)

            if size is None:
                continue

            try:
                path.unlink()
            except OSError as error:
                logger.warning("archive.delete.failed", path=str(path), error=str(error))
                continue

            result.deleted_files += 1
            result.deleted_bytes += size

            if (
                stop_at_target
                and result.deleted_files % 50 == 0
                and free_gb(settings.data_dir) >= settings.archive_free_target_gb
            ):
                return


def oldest_day_directories() -> list[tuple[str, Path, str]]:
    """Every (date, directory, node_id) under captures/ and originals/, oldest date first."""
    settings = get_settings()
    found: list[tuple[str, Path, str]] = []

    for source_name in DELETABLE_SOURCES:
        if source_name not in configured_sources():
            continue

        root = settings.data_dir / source_name

        if not root.is_dir():
            continue

        for node_dir in root.iterdir():
            if not node_dir.is_dir():
                continue

            for date_dir in node_dir.iterdir():
                if date_dir.is_dir() and DATE_DIR.match(date_dir.name):
                    found.append((date_dir.name, date_dir, node_dir.name))

    found.sort(key=lambda item: (item[0], str(item[1])))

    return found


def prune_empty(directory: Path) -> None:
    for current, _directories, _files in os.walk(directory, topdown=False):
        try:
            Path(current).rmdir()
        except OSError:
            pass


# ---------- cycle ----------


def run_cycle() -> dict:
    """One copy pass and one cleanup pass. Safe to call from anywhere, one at a time."""
    if not _cycle_lock.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}

    _state["running"] = True
    result = CycleResult()

    try:
        problem = archive_problem()

        if problem:
            _state["last_error"] = problem
            logger.warning("archive.unavailable", reason=problem)
            return {"skipped": problem}

        _state["last_error"] = None
        copy_phase(result, deadline=time.monotonic() + CYCLE_BUDGET_SECONDS)
        delete_phase(result)

        if result.copied_files or result.deleted_files or result.failed_files:
            logger.info("archive.cycle", **result.as_dict())

        _state["last_result"] = result.as_dict()

        return result.as_dict()

    except Exception as error:
        _state["last_error"] = str(error)
        logger.exception("archive.cycle.failed", error=str(error))

        return {"error": str(error)}

    finally:
        _state["last_run_at"] = utc_now().isoformat()
        _state["running"] = False
        _cycle_lock.release()


def status() -> dict:
    settings = get_settings()
    problem = archive_problem()

    with SessionLocal() as db:
        archived_count = db.query(ArchivedFile).count()

    payload = {
        "configured": settings.archive_dir is not None,
        "archive_dir": str(settings.archive_dir) if settings.archive_dir else None,
        "ready": problem is None,
        "problem": problem,
        "sources": configured_sources(),
        "free_gb_local": round(free_gb(settings.data_dir), 2),
        "free_trigger_gb": settings.archive_free_trigger_gb,
        "free_target_gb": settings.archive_free_target_gb,
        "delete_after_days": settings.archive_delete_after_days,
        "archived_files": archived_count,
        **_state,
    }

    if problem is None:
        try:
            payload["free_gb_archive"] = round(free_gb(settings.archive_dir), 2)
        except OSError:
            payload["free_gb_archive"] = None

    return payload
