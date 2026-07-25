from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class NodeCaptureState(Base):
    """Details of the most recent capture a node uploaded.

    The capture list itself is derived from the filesystem, but the per-frame
    metadata the node reports (actual exposure, gain, mean, sensor temperature)
    only existed for the duration of the upload request. Keeping the latest copy
    lets the overlay editor preview real values instead of invented samples.
    """

    __tablename__ = "node_capture_state"

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sequence_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    image_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
