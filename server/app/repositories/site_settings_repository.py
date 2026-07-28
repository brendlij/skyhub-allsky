from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.site_settings import SiteSettings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SiteSettingsRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self) -> SiteSettings:
        """The site, seeded from the environment on first use.

        Seeding rather than defaulting to nothing: an install that already set
        SKYHUB_SERVER_LATITUDE keeps working without the operator having to
        re-enter it, and every install after this one starts from a value it can
        see in the UI rather than one buried in a config file.
        """
        site = self.db.get(SiteSettings, "default")

        if site is None:
            settings = get_settings()

            site = SiteSettings(
                settings_id="default",
                label="",
                latitude=settings.latitude,
                longitude=settings.longitude,
                elevation_m=0.0,
                timezone=settings.timezone,
                updated_at=utc_now(),
            )
            self.db.add(site)
            self.db.commit()
            self.db.refresh(site)

        return site

    def update(self, values: dict[str, Any]) -> SiteSettings:
        site = self.get_or_create()

        for field_name, value in values.items():
            setattr(site, field_name, value)

        site.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(site)

        return site
