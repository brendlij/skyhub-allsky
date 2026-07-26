from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.overlay_preset import OverlayPreset


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Everything that describes how a label looks and where it sits. Entity ids are
# deliberately dropped: they identify one label in one node's overlay, and the
# editor mints fresh ones each time a preset is applied.
PRESET_ENTITY_FIELDS = (
    "label",
    "text",
    "anchor",
    "x",
    "y",
    "font_size",
    "color",
    "background",
    "background_opacity",
    "enabled",
)


def sanitize_entities(entities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned = []

    for entity in entities or []:
        if not isinstance(entity, dict):
            continue

        kept = {key: entity[key] for key in PRESET_ENTITY_FIELDS if key in entity}
        kept["type"] = "text"
        cleaned.append(kept)

    return cleaned


def preset_to_dict(preset: OverlayPreset) -> dict[str, Any]:
    return {
        "id": preset.id,
        "name": preset.name,
        "description": preset.description or "",
        "builtin": False,
        "entities": [dict(entity) for entity in preset.entities or []],
        "created_at": preset.created_at.isoformat() if preset.created_at else None,
        "updated_at": preset.updated_at.isoformat() if preset.updated_at else None,
    }


class OverlayPresetRepository:
    def __init__(self, db: Session):
        self.db = db

    def list(self) -> list[OverlayPreset]:
        return list(
            self.db.scalars(select(OverlayPreset).order_by(OverlayPreset.name)).all()
        )

    def get(self, preset_id: str) -> OverlayPreset | None:
        return self.db.get(OverlayPreset, preset_id)

    def get_by_name(self, name: str) -> OverlayPreset | None:
        return self.db.scalars(
            select(OverlayPreset).where(OverlayPreset.name == name)
        ).first()

    def create(
        self,
        name: str,
        entities: list[dict[str, Any]],
        description: str = "",
    ) -> OverlayPreset:
        preset = OverlayPreset(
            id=f"custom-{uuid4().hex[:12]}",
            name=name,
            description=description,
            entities=sanitize_entities(entities),
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        self.db.add(preset)
        self.db.commit()
        self.db.refresh(preset)

        return preset

    def update(self, preset: OverlayPreset, values: dict[str, Any]) -> OverlayPreset:
        if "name" in values:
            preset.name = values["name"]

        if "description" in values:
            preset.description = values["description"] or ""

        if "entities" in values:
            preset.entities = sanitize_entities(values["entities"])

        preset.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(preset)

        return preset

    def delete(self, preset: OverlayPreset) -> None:
        self.db.delete(preset)
        self.db.commit()
