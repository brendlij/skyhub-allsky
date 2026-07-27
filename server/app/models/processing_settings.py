from datetime import datetime

from sqlalchemy import Boolean, DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProcessingSettings(Base):
    """Per-processor configuration, one row per processor.

    The settings themselves live in a JSON blob rather than in columns, because a
    processor's options are its own business and adding one must not need a schema
    change. Each processor declares its defaults and validates its own config; the
    row only stores the operator's overrides on top.
    """

    __tablename__ = "processing_settings"

    processor: Mapped[str] = mapped_column(String(50), primary_key=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Lower runs first. Only meaningful as a tie-break: processors are independent
    # by design, so this decides who gets the CPU first, not who depends on whom.
    priority: Mapped[int] = mapped_column(default=100, nullable=False)

    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
