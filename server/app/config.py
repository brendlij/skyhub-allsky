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
    def frontend_dist_dir(self) -> Path:
        return REPO_ROOT / "frontend" / "dist"


@lru_cache
def get_settings() -> ServerSettings:
    return ServerSettings()
