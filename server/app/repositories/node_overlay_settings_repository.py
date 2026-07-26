from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.node_overlay_settings import NodeOverlaySettings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Font sizes are in source-image pixels. An allsky frame is thousands of pixels
# wide, so a 30px label is invisible once the frame is viewed at any sane size.
def default_overlay_entities() -> list[dict[str, Any]]:
    return [
        {
            "id": "overlay-datetime",
            "type": "text",
            "label": "Date + time",
            "enabled": True,
            "x": 0.035,
            "y": 0.055,
            "anchor": "top-left",
            "font_size": 108,
            "color": "#ffffff",
            "background": "#000000",
            "background_opacity": 0.45,
            "text": "$capture.datetime",
        },
        {
            "id": "overlay-period",
            "type": "text",
            "label": "Period",
            "enabled": True,
            "x": 0.035,
            "y": 0.925,
            "anchor": "bottom-left",
            "font_size": 96,
            "color": "#ffffff",
            "background": "#000000",
            "background_opacity": 0.35,
            "text": "$capture.period",
        },
        {
            "id": "overlay-environment",
            "type": "text",
            "label": "Environment",
            "enabled": True,
            "x": 0.965,
            "y": 0.055,
            "anchor": "top-right",
            "font_size": 88,
            "color": "#ffffff",
            "background": "#000000",
            "background_opacity": 0.35,
            "text": "$bme280.temperature C / $bme280.humidity %",
        },
    ]


class NodeOverlaySettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, node_id: str) -> NodeOverlaySettings | None:
        return self.db.get(NodeOverlaySettings, node_id)

    def get_or_create(self, node_id: str) -> NodeOverlaySettings:
        settings = self.get(node_id)

        if settings is None:
            settings = NodeOverlaySettings(
                node_id=node_id,
                enabled=True,
                entities=default_overlay_entities(),
                updated_at=utc_now(),
            )
            self.db.add(settings)
            self.db.commit()
            self.db.refresh(settings)

        return settings

    def update(self, node_id: str, values: dict[str, Any]) -> NodeOverlaySettings:
        settings = self.get_or_create(node_id)

        if "enabled" in values:
            settings.enabled = bool(values["enabled"])

        if "entities" in values:
            settings.entities = values["entities"] or []

        settings.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(settings)

        return settings
