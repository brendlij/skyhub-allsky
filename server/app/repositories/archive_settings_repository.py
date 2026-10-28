from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.archive_settings import ArchiveSettings

EDITABLE = {
    "enabled",
    "archive_dir",
    "interval_minutes",
    "sources",
    "free_trigger_gb",
    "free_target_gb",
    "delete_after_days",
    "require_mount",
}


class ArchiveSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self) -> ArchiveSettings:
        row = self.db.get(ArchiveSettings, 1)

        if row is not None:
            return row

        seed = get_settings()
        row = ArchiveSettings(
            settings_id=1,
            enabled=seed.archive_dir is not None,
            archive_dir=str(seed.archive_dir) if seed.archive_dir else None,
            interval_minutes=seed.archive_interval_minutes,
            sources=seed.archive_sources,
            free_trigger_gb=seed.archive_free_trigger_gb,
            free_target_gb=seed.archive_free_target_gb,
            delete_after_days=seed.archive_delete_after_days,
            require_mount=seed.archive_require_mount,
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)

        return row

    def update(self, values: dict[str, Any]) -> ArchiveSettings:
        row = self.get_or_create()

        for key, value in values.items():
            if key in EDITABLE:
                setattr(row, key, value)

        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)

        return row
