from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.node_device_settings import NodeDeviceSettings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_devices() -> dict[str, Any]:
    return {
        "environment": {
            "driver": "bme280",
            "interval_seconds": 30,
            "bme280_i2c_bus": 1,
            "bme280_i2c_address": "0x77",
        },
        "heater": {
            "driver": "gpiozero",
            "gpio_pin": 23,
            "active_high": True,
            # "manual": the heater follows the switch in the UI. "auto": the node
            # switches it from the dew point spread (temperature minus dew point).
            "mode": "manual",
            "auto": {
                "on_margin_c": 3.0,
                "off_margin_c": 5.0,
                "min_switch_seconds": 120,
            },
            "pwm": {
                "enabled": False,
                "duty_cycle": 1.0,
            },
        },
    }


def deep_merge(base: dict, update: dict) -> dict:
    merged = deepcopy(base)

    for key, value in (update or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value

    return merged


class NodeDeviceSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, node_id: str) -> NodeDeviceSettings | None:
        return self.db.get(NodeDeviceSettings, node_id)

    def get_or_create(self, node_id: str) -> NodeDeviceSettings:
        settings = self.get(node_id)

        if settings is None:
            settings = NodeDeviceSettings(
                node_id=node_id,
                devices=default_devices(),
                updated_at=utc_now(),
            )
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)
            return settings

        normalized = deep_merge(default_devices(), settings.devices or {})

        if normalized != settings.devices:
            settings.devices = normalized
            settings.updated_at = utc_now()
            self.db.commit()
            self.db.refresh(settings)

        return settings

    def update(self, node_id: str, devices: dict[str, Any]) -> NodeDeviceSettings:
        settings = self.get_or_create(node_id)
        settings.devices = deep_merge(default_devices(), devices or {})
        settings.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(settings)
        return settings
