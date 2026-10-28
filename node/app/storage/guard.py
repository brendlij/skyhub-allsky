import shutil
from pathlib import Path


class DiskGuard:
    """Stops capturing before the disk fills, and only restarts with real headroom.

    A full disk is not a clean failure on a Pi: SQLite cannot commit, logins return
    500, and the capture loop dies mid-write. Pausing while there is still room
    keeps every other part of the system alive.

    The two thresholds are deliberately apart. With a single one, a disk hovering at
    the limit would flap between capturing and paused on every frame.
    """

    def __init__(self, path: Path, stop_below_gb: float, resume_above_gb: float):
        self.path = path
        self.stop_below_gb = stop_below_gb
        self.resume_above_gb = max(resume_above_gb, stop_below_gb)
        self.paused = False

    @property
    def enabled(self) -> bool:
        return self.stop_below_gb > 0

    def free_gb(self) -> float:
        return shutil.disk_usage(self.path).free / 1024**3

    def check(self) -> tuple[bool, float]:
        """Return (paused, free_gb) and update the paused state."""
        free = self.free_gb()

        if not self.enabled:
            return False, free

        if self.paused:
            if free >= self.resume_above_gb:
                self.paused = False
        elif free < self.stop_below_gb:
            self.paused = True

        return self.paused, free
