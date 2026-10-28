from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent


class ServerSettings(BaseSettings):
    app_name: str = "SkyHub Server"
    app_version: str = "0.1.0"
    data_dir: Path = REPO_ROOT / "data"
    database_filename: str = "skyhub.db"
    # Seed values only, read exactly once: they fill in the site_settings row the
    # first time it is created, and nothing consults them afterwards. The live
    # location is the row, edited in Settings and served by `app.astro`, which is
    # the only thing any sun calculation may ask. Changing these on an install
    # that already has a row does nothing - change the location in the UI.
    latitude: float = 52.52
    longitude: float = 13.405
    timezone: str = "Europe/Berlin"
    # Empty means no authentication, which is the pre-existing behaviour and fine
    # on a trusted LAN. Set it to require a key on every /api route and WebSocket.
    api_key: str = ""
    # Read-only access to the current image alone, so a website or a shared
    # dashboard never has to carry the api_key - which can change settings, stop
    # capture and delete nodes. Either open it outright, or hand out a token that
    # unlocks nothing else. Both ignored unless api_key is set.
    public_captures: bool = False
    public_capture_token: str = ""

    # The API key is a machine credential: nodes and scripts cannot hold a session
    # or answer a TOTP prompt. Left at False it also opens the rest of the API, so
    # existing automation keeps working after the upgrade. Set it to True and the
    # key unlocks only the node routes, making human and machine access fully
    # disjoint - the stricter posture, at the cost of any script using /api/nodes.
    api_key_nodes_only: bool = False

    # Sessions. Idle expiry catches an abandoned browser; the absolute cap bounds
    # how long a single stolen cookie stays useful no matter how active it is.
    session_idle_minutes: int = 30
    session_absolute_hours: int = 24
    trusted_device_days: int = 30

    # Login backoff. Delay doubles per consecutive failure from the base, capped -
    # slow enough to make guessing hopeless, short enough that a fat-fingered
    # password does not lock the operator out of their own camera for an hour.
    login_backoff_base_seconds: int = 2
    login_backoff_max_seconds: int = 300
    login_attempt_window_minutes: int = 15

    # Sent by a proxy in front of SkyHub. Only consulted when set, because an
    # unvalidated X-Forwarded-For is trivially spoofed and would let an attacker
    # dodge per-IP throttling by inventing a new address per request.
    trust_proxy_headers: bool = False

    # Post-processing. The queue is bounded because a processor that falls behind
    # must cost frames, not memory - on a Pi, an unbounded backlog of decoded
    # images is an out-of-memory kill that takes the capture down with it.
    # Hard floor for uploads: below this many GB free the server refuses new frames
    # with 507 instead of writing into a disk SQLite also needs. Kept under the
    # node's own pause threshold so the node normally stops first. 0 turns it off.
    min_free_disk_gb: float = 3.0

    # Archive to a second disk or a mounted network share (NAS). Empty disables it.
    # Frames are copied, read back and hash-checked before anything local is ever
    # deleted. See docs/ARCHIVE.md for mounting a share.
    archive_dir: Path | None = None
    archive_interval_minutes: int = 10
    # Which trees are copied. Only captures and originals are ever deleted locally;
    # derived products are referenced by database rows, so they are copied only.
    archive_sources: str = "originals,captures,derived"
    # Local copies are removed, oldest day first, once free space drops below the
    # trigger, until it is back above the target. Only verified files, never today's.
    archive_free_trigger_gb: float = 15.0
    archive_free_target_gb: float = 25.0
    # Also remove verified local copies older than this many days. 0 = only when
    # space is low.
    archive_delete_after_days: int = 0
    # Refuse an archive that lives on the same filesystem as the data directory.
    # An unmounted share is just an empty folder on the SD card: copying into it
    # fills the card and then looks like a successful backup.
    archive_require_mount: bool = True
    processing_enabled: bool = True
    processing_queue_size: int = 64
    # Where ffmpeg lives, when it is not on PATH. Empty means "look it up".
    ffmpeg_path: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SKYHUB_SERVER_",
    )

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_filename

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def masks_dir(self) -> Path:
        return self.data_dir / "masks"

    @property
    def derived_dir(self) -> Path:
        """Finished products: startrails, keograms, timelapses, archives."""
        return self.data_dir / "derived"

    @property
    def processing_state_dir(self) -> Path:
        """Working state a session rebuilds from - stacks, strips, frame lists.

        Separate from derived/ so it can be wiped without losing a night's output,
        and so retention never has to tell the two apart.
        """
        return self.data_dir / "processing"

    @property
    def frontend_dist_dir(self) -> Path:
        return REPO_ROOT / "frontend" / "dist"


@lru_cache
def get_settings() -> ServerSettings:
    return ServerSettings()
