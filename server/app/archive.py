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

The configuration lives in the `archive_settings` row and is edited in the UI. The
archive path is only ever a copy destination: nothing in here deletes anything under
it, so a wrong path costs a pointless copy, never data.
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
from app.repositories.archive_settings_repository import ArchiveSettingsRepository
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
ALL_SOURCES = ("originals", "captures", "derived")
DELETABLE_SOURCES = ("captures", "originals")
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WRITE_TEST_NAME = ".skyhub-write-test"

_cycle_lock = threading.Lock()
_state: dict = {"running": False, "last_run_at": None, "last_result": None, "last_error": None}


class ArchiveError(Exception):
    """A copy that did not verify. The partial file is already gone when this is raised."""


@dataclass(frozen=True)
class ArchiveConfig:
    enabled: bool
    archive_dir: Path | None
    interval_minutes: int
    sources: tuple[str, ...]
    free_trigger_gb: float
    free_target_gb: float
    delete_after_days: int
    require_mount: bool

    @property
    def active(self) -> bool:
        return self.enabled and self.archive_dir is not None

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "archive_dir": str(self.archive_dir) if self.archive_dir else "",
            "interval_minutes": self.interval_minutes,
            "sources": list(self.sources),
            "free_trigger_gb": self.free_trigger_gb,
            "free_target_gb": self.free_target_gb,
            "delete_after_days": self.delete_after_days,
            "require_mount": self.require_mount,
        }


def parse_sources(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    names = value.split(",") if isinstance(value, str) else list(value)
    cleaned = [name.strip() for name in names]

    return tuple(name for name in ALL_SOURCES if name in cleaned)


def config_from_row(row) -> ArchiveConfig:
    return ArchiveConfig(
        enabled=bool(row.enabled),
        archive_dir=Path(row.archive_dir) if row.archive_dir else None,
        interval_minutes=max(1, int(row.interval_minutes)),
        sources=parse_sources(row.sources),
        free_trigger_gb=float(row.free_trigger_gb),
        free_target_gb=float(row.free_target_gb),
        delete_after_days=max(0, int(row.delete_after_days)),
        require_mount=bool(row.require_mount),
    )


def load_config() -> ArchiveConfig:
    with SessionLocal() as db:
        return config_from_row(ArchiveSettingsRepository(db).get_or_create())


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


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1024**3


def directory_problem(path: Path | None, require_mount: bool) -> str | None:
    """Why this directory cannot be used as the archive, or None when it can."""
    data_dir = get_settings().data_dir

    if path is None:
        return "No destination folder is set."

    if not path.is_absolute():
        return f"{path} is not an absolute path."

    if not path.is_dir():
        return f"{path} does not exist. Is the share mounted?"

    if not os.access(path, os.W_OK):
        return f"{path} is not writable."

    if require_mount:
        try:
            same_disk = path.stat().st_dev == data_dir.stat().st_dev
        except OSError as error:
            return f"Cannot read {path}: {error}"

        if same_disk:
            return (
                f"{path} is on the same disk as the data folder. A share that is not "
                "mounted looks exactly like this, so it is refused. Mount the share, or "
                "untick the mount check if this really is a separate location."
            )

    return None


def archive_problem(config: ArchiveConfig) -> str | None:
    if config.archive_dir is None:
        return "No destination folder is set."

    return directory_problem(config.archive_dir, config.require_mount)


def test_destination(path_text: str, require_mount: bool) -> dict:
    """Check a candidate destination the way the archiver will use it.

    Writes and reads back a small file, because "the folder exists" is also true of
    a share that has gone read-only or full.
    """
    checks: list[dict] = []
    path = Path(path_text.strip()) if path_text and path_text.strip() else None
    problem = directory_problem(path, require_mount)

    checks.append({"label": "Folder is usable", "ok": problem is None, "detail": problem or str(path)})

    if problem is None and path is not None:
        probe = path / WRITE_TEST_NAME
        token = hashlib.sha256(str(time.time()).encode()).hexdigest()

        try:
            probe.write_text(token)
            readable = probe.read_text() == token
            checks.append(
                {
                    "label": "Can write and read back",
                    "ok": readable,
                    "detail": "OK" if readable else "The file read back differently",
                }
            )
        except OSError as error:
            checks.append({"label": "Can write and read back", "ok": False, "detail": str(error)})
        finally:
            probe.unlink(missing_ok=True)

        try:
            checks.append(
                {
                    "label": "Free space",
                    "ok": free_gb(path) > 1,
                    "detail": f"{free_gb(path):.1f} GB free",
                }
            )
        except OSError as error:
            checks.append({"label": "Free space", "ok": False, "detail": str(error)})

    return {"ok": all(check["ok"] for check in checks), "checks": checks}


SYSTEM_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/var/lib")
USABLE_FILESYSTEMS = {
    "cifs", "smb3", "nfs", "nfs4", "ext4", "exfat", "vfat", "ntfs", "ntfs3", "fuseblk", "btrfs", "xfs",
}


def mount_candidates() -> list[dict]:
    """Mounted filesystems that could plausibly be an archive: shares and extra drives."""
    mounts_file = Path("/proc/mounts")

    if not mounts_file.is_file():
        return []

    found: list[dict] = []

    for line in mounts_file.read_text(errors="replace").splitlines():
        parts = line.split()

        if len(parts) < 3:
            continue

        source, mount_point, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]

        if fstype not in USABLE_FILESYSTEMS or mount_point == "/":
            continue

        if mount_point.startswith(SYSTEM_MOUNT_PREFIXES):
            continue

        try:
            free = round(free_gb(Path(mount_point)), 1)
        except OSError:
            continue

        found.append({"path": mount_point, "type": fstype, "source": source, "free_gb": free})

    return found


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


def copy_phase(config: ArchiveConfig, result: CycleResult, deadline: float) -> None:
    data_dir = get_settings().data_dir
    settle_before = time.time() - SETTLE_SECONDS
    consecutive_failures = 0

    with SessionLocal() as db:
        for source_name in config.sources:
            root = data_dir / source_name

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

                        size, digest = copy_verified(path, config.archive_dir / rel)

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


def verified_on_archive(config: ArchiveConfig, db, path: Path) -> int | None:
    """Size of the local file when a verified archive copy of it exists, else None."""
    try:
        local_size = path.stat().st_size
        rel = relative(path)
        row = db.get(ArchivedFile, rel)

        if row is None or row.size_bytes != local_size:
            return None

        if (config.archive_dir / rel).stat().st_size != local_size:
            return None

    except OSError:
        return None

    return local_size


def delete_phase(config: ArchiveConfig, result: CycleResult) -> None:
    data_dir = get_settings().data_dir
    freeing = free_gb(data_dir) < config.free_trigger_gb
    cutoff = None

    if config.delete_after_days > 0:
        cutoff = (utc_now() - timedelta(days=config.delete_after_days)).date().isoformat()

    if not freeing and cutoff is None:
        return

    if freeing:
        result.notes.append(
            f"free space below {config.free_trigger_gb:g} GB, freeing to {config.free_target_gb:g} GB"
        )

    today = astro.archive_period(utc_now())[0]
    busy = protected_dates()

    with SessionLocal() as db:
        # Oldest first, so the first day that is neither too old nor needed for space
        # means every later day is not either.
        for date_name, directory, node_id in oldest_day_directories(config):
            if date_name >= today or (node_id, date_name) in busy:
                continue

            too_old = cutoff is not None and date_name < cutoff

            if freeing and free_gb(data_dir) >= config.free_target_gb:
                freeing = False

            if not too_old and not freeing:
                break

            delete_directory(config, db, directory, result, stop_at_target=not too_old)
            prune_empty(directory)


def delete_directory(
    config: ArchiveConfig, db, directory: Path, result: CycleResult, stop_at_target: bool
) -> None:
    """Remove every verified file under a day directory, optionally stopping once there is room."""
    data_dir = get_settings().data_dir

    for current, _directories, files in os.walk(directory):
        for name in sorted(files):
            path = Path(current) / name
            size = verified_on_archive(config, db, path)

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
                and free_gb(data_dir) >= config.free_target_gb
            ):
                return


def oldest_day_directories(config: ArchiveConfig) -> list[tuple[str, Path, str]]:
    """Every (date, directory, node_id) under captures/ and originals/, oldest date first."""
    data_dir = get_settings().data_dir
    found: list[tuple[str, Path, str]] = []

    for source_name in DELETABLE_SOURCES:
        if source_name not in config.sources:
            continue

        root = data_dir / source_name

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


def run_cycle(force: bool = False) -> dict:
    """One copy pass and one cleanup pass. Safe to call from anywhere, one at a time.

    `force` runs even when the archive is switched off, for a manual "run now".
    """
    if not _cycle_lock.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}

    _state["running"] = True
    result = CycleResult()

    try:
        config = load_config()

        if not config.enabled and not force:
            return {"skipped": "the archive is switched off"}

        problem = archive_problem(config)

        if problem:
            _state["last_error"] = problem
            logger.warning("archive.unavailable", reason=problem)
            return {"skipped": problem}

        _state["last_error"] = None
        copy_phase(config, result, deadline=time.monotonic() + CYCLE_BUDGET_SECONDS)
        delete_phase(config, result)

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


def due(config: ArchiveConfig) -> bool:
    """Whether the background loop should start a pass now."""
    if not config.active:
        return False

    last = _state["last_run_at"]

    if last is None:
        return True

    elapsed = (utc_now() - datetime.fromisoformat(last)).total_seconds()

    return elapsed >= config.interval_minutes * 60


def status() -> dict:
    config = load_config()
    problem = archive_problem(config) if config.archive_dir else None

    with SessionLocal() as db:
        archived_count = db.query(ArchivedFile).count()

    payload = {
        "configured": config.archive_dir is not None,
        "enabled": config.enabled,
        "ready": config.active and problem is None,
        "problem": problem,
        "free_gb_local": round(free_gb(get_settings().data_dir), 2),
        "archived_files": archived_count,
        **_state,
    }

    if config.archive_dir is not None and problem is None:
        try:
            payload["free_gb_archive"] = round(free_gb(config.archive_dir), 2)
        except OSError:
            payload["free_gb_archive"] = None

    return payload
