from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


NODE_DIR = Path(__file__).resolve().parents[1]


class NodeSettings(BaseSettings):
    node_id: str = "pi-hqcam-01"
    server_ws_base_url: str = "ws://127.0.0.1:8000/ws/nodes"
    server_http_base_url: str | None = None
    # Must match SKYHUB_SERVER_API_KEY when the server has one set. Empty is fine
    # against a server without authentication.
    api_key: str = ""
    camera_driver: str = "mock"
    heartbeat_interval_seconds: int = 10
    reconnect_initial_delay_seconds: float = 1
    reconnect_max_delay_seconds: float = 30
    captures_dir: Path = NODE_DIR / "data" / "captures"
    environment_sensor_driver: str = "mock"
    environment_interval_seconds: int = 30
    bme280_i2c_bus: int = 1
    bme280_i2c_address: str = "0x77"
    heater_driver: str = "gpiozero"
    heater_gpio_pin: int = 23
    heater_active_high: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SKYHUB_NODE_",
    )

    @property
    def server_ws_url(self) -> str:
        return f"{self.server_ws_base_url.rstrip('/')}/{self.node_id}"

    @property
    def upload_url(self) -> str:
        if self.server_http_base_url:
            base_url = self.server_http_base_url
        elif self.server_ws_base_url.startswith("wss://"):
            base_url = "https://" + self.server_ws_base_url.removeprefix("wss://")
        elif self.server_ws_base_url.startswith("ws://"):
            base_url = "http://" + self.server_ws_base_url.removeprefix("ws://")
        else:
            base_url = self.server_ws_base_url

        base_url = base_url.rstrip("/")

        if base_url.endswith("/ws/nodes"):
            base_url = base_url.removesuffix("/ws/nodes")

        return f"{base_url}/api/captures/upload"

    @property
    def api_headers(self) -> dict[str, str]:
        """Auth headers for the server, empty when no key is configured."""
        return {"X-API-Key": self.api_key} if self.api_key else {}

    @property
    def bme280_i2c_address_int(self) -> int:
        return int(str(self.bme280_i2c_address), 0)


@lru_cache
def get_settings() -> NodeSettings:
    return NodeSettings()
