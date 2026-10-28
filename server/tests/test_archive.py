"""Checks for the archiver: copy, verification, and deleting only from verified copies.

Runs against throwaway data and archive directories.

Run with:  python server/tests/test_archive.py
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

_DATA = Path(tempfile.mkdtemp(prefix="skyhub-archive-data-"))
_ARCHIVE = Path(tempfile.mkdtemp(prefix="skyhub-archive-dest-"))
os.environ["SKYHUB_SERVER_DATA_DIR"] = str(_DATA)
os.environ["SKYHUB_SERVER_ARCHIVE_DIR"] = str(_ARCHIVE)
# Both scratch folders share a filesystem; the mount check has its own test below.
os.environ["SKYHUB_SERVER_ARCHIVE_REQUIRE_MOUNT"] = "false"

from app import archive, astro  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.database import SessionLocal, create_db_tables  # noqa: E402
from app.models.archived_file import ArchivedFile  # noqa: E402
from app.repositories.archive_settings_repository import ArchiveSettingsRepository  # noqa: E402

settings = get_settings()


def set_config(**values) -> None:
    """Edit the archive settings the way the UI does: through the saved row."""
    with SessionLocal() as db:
        ArchiveSettingsRepository(db).update(values)


failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return

    failures.append(f"{label}{f' - {detail}' if detail else ''}")
    print(f"  FAIL  {label} {detail}")


def make_file(relative: str, content: bytes, age_seconds: float = 3600) -> Path:
    path = _DATA / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))

    return path


create_db_tables()
TODAY = astro.archive_period(datetime.now(timezone.utc))[0]
OLD = "2020-01-01"

old_original = make_file(f"originals/cam/{OLD}/night/a.jpg", b"original-bytes" * 1000)
old_capture = make_file(f"captures/cam/{OLD}/night/a.jpg", b"capture-bytes" * 1000)
old_derived = make_file(f"derived/cam/{OLD}/keogram.jpg", b"derived-bytes" * 1000)
today_capture = make_file(f"captures/cam/{TODAY}/day/b.jpg", b"today" * 1000)
young_capture = make_file(f"captures/cam/{OLD}/night/young.jpg", b"young" * 1000, age_seconds=5)

print("copy")
result = archive.run_cycle()
check("a cycle ran", "copied_files" in result, str(result))
check("settled files are copied", result.get("copied_files") == 4, str(result))
check(
    "the copy is byte-identical",
    (_ARCHIVE / f"originals/cam/{OLD}/night/a.jpg").read_bytes() == old_original.read_bytes(),
)
check("derived products are copied too", (_ARCHIVE / f"derived/cam/{OLD}/keogram.jpg").exists())
check("a file still being written is skipped", not (_ARCHIVE / f"captures/cam/{OLD}/night/young.jpg").exists())
check("no partial files are left", not list(_ARCHIVE.rglob("*.part")))

with SessionLocal() as db:
    check("verified files are recorded", db.query(ArchivedFile).count() == 4)

print("an unchanged tree is not copied twice")
check("second cycle copies nothing", archive.run_cycle().get("copied_files") == 0)

print("nothing is deleted while there is room")
check("plenty of space keeps every local file", old_original.exists() and old_capture.exists())

print("verification failures")
bad_source = make_file(f"captures/cam/{OLD}/night/bad.jpg", b"bad" * 1000)
real_sha = archive.sha256_of
archive.sha256_of = lambda path: "0" * 64
try:
    archive.copy_verified(bad_source, _ARCHIVE / "captures/cam/bad.jpg")
    raised = False
except archive.ArchiveError:
    raised = True
finally:
    archive.sha256_of = real_sha

check("a copy that does not verify is rejected", raised)
check("and leaves nothing behind", not (_ARCHIVE / "captures/cam/bad.jpg").exists())
check("no .part either", not list(_ARCHIVE.rglob("*.part")))

print("deleting from verified copies")
set_config(free_trigger_gb=1e9, free_target_gb=2e9)
result = archive.run_cycle()
check("old verified files are deleted", not old_original.exists() and not old_capture.exists(), str(result))
check("today's frames are never deleted", today_capture.exists())
check("a file never copied is never deleted", young_capture.exists())
check("derived products are never deleted", old_derived.exists())
check("the archive copy survives", (_ARCHIVE / f"originals/cam/{OLD}/night/a.jpg").exists())

print("a missing or wrong archive copy blocks deletion")
set_config(free_trigger_gb=15.0, free_target_gb=25.0)
guarded = make_file(f"originals/cam/{OLD}/night/guarded.jpg", b"guarded" * 1000)
archive.run_cycle()
(_ARCHIVE / f"originals/cam/{OLD}/night/guarded.jpg").write_bytes(b"short")
set_config(free_trigger_gb=1e9, free_target_gb=2e9)
archive.run_cycle()
check("a truncated archive copy keeps the local file", guarded.exists())

print("archive unavailable")
set_config(archive_dir=str(_ARCHIVE / "not-mounted"))
survivor = make_file(f"originals/cam/{OLD}/night/survivor.jpg", b"survivor" * 1000)
result = archive.run_cycle()
check("an absent archive skips the cycle", "skipped" in result, str(result))
check("and deletes nothing", survivor.exists() and guarded.exists())
set_config(archive_dir=str(_ARCHIVE))

print("mount check")
set_config(require_mount=True)
check("an archive on the same filesystem is refused", archive.archive_problem(archive.load_config()) is not None)
set_config(require_mount=False)

print("switched off")
set_config(enabled=False)
check("a switched-off archive does not run", "skipped" in archive.run_cycle())
check("but a manual run still can", "copied_files" in archive.run_cycle(force=True))
set_config(enabled=True)

print("destination test")
good = archive.test_destination(str(_ARCHIVE), require_mount=False)
check("a writable folder passes", good["ok"], str(good))
check("the probe file is cleaned up", not (_ARCHIVE / archive.WRITE_TEST_NAME).exists())
check("a missing folder fails", not archive.test_destination(str(_ARCHIVE / "nope"), require_mount=False)["ok"])
check("a relative path fails", not archive.test_destination("relative/dir", require_mount=False)["ok"])
check("an empty path fails", not archive.test_destination("", require_mount=False)["ok"])
check("the same-disk check applies to the test", not archive.test_destination(str(_ARCHIVE), require_mount=True)["ok"])
check("mount listing never raises", isinstance(archive.mount_candidates(), list))

print("status")
state = archive.status()
check("status reports ready", state["ready"] is True and state["configured"] is True, str(state))
check("the loop is due on a fresh start", archive.due(archive.load_config()) in (True, False))

if failures:
    print(f"\n{len(failures)} check(s) failed:")

    for failure in failures:
        print(f"  - {failure}")

    sys.exit(1)

print("\nall checks passed")
