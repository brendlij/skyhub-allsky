from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ArchiveSettings(Base):
    """The archive configuration, edited in the UI. One row.

    The SKYHUB_SERVER_ARCHIVE_* environment variables only seed this row the first
    time it is created; after that the row is the source of truth, so a change in
    Settings is never undone by a stale service file.
    """

    __tablename__ = "archive_settings"

    settings_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    archive_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    sources: Mapped[str] = mapped_column(String(100), nullable=False, default="originals,captures,derived")
    free_trigger_gb: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    free_target_gb: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    delete_after_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    require_mount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
