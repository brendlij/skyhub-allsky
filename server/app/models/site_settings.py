from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class SiteSettings(Base):
    """Where the camera is. One row, like the storage policy.

    The coordinates used to come from the environment, which meant that unless
    someone set two variables before the first start, every sun calculation in
    the system - the elevation printed on each frame, and now the window a
    startrail stacks in - was computed for a default nobody chose. That is not a
    deployment detail; it is a setting, and it belongs where the operator can see
    and change it.
    """

    __tablename__ = "site_settings"

    settings_id: Mapped[str] = mapped_column(String(50), primary_key=True, default="default")

    # Free text, purely for the operator - "garden shed", "Feldberg". Astral
    # takes a name but does nothing with it.
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    # Metres above sea level. Astral uses it to correct for refraction, which
    # moves sunrise by a minute or so at altitude and is irrelevant at the
    # depressions a startrail cares about - but it costs one column to be right.
    elevation_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    timezone: Mapped[str] = mapped_column(String(80), nullable=False)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
