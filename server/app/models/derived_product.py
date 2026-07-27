from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class DerivedProduct(Base):
    """Anything the pipeline produced from captures: an image, a video, an archive.

    One row per (node, date, period, kind), replaced in place as a live product is
    refreshed - a live keogram updates hundreds of times a night and must not
    leave hundreds of rows behind. `state` distinguishes the two lives of a
    product: "live" while it is still growing, "final" once its session closed.
    """

    __tablename__ = "derived_products"

    product_id: Mapped[str] = mapped_column(String(250), primary_key=True)

    node_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    archive_date: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(20), nullable=False)

    # The session this came out of. Nullable because a product can outlive the
    # session row that produced it - retention prunes sessions and products on
    # their own schedules.
    session_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    # The processor that made it ("startrail") and what this particular output is
    # ("startrail", "startrail_build", "keogram", "timelapse"). One processor can
    # emit several kinds, which is why they are separate columns.
    processor: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # The retention and grouping bucket. Deliberately coarser than `kind`: a rule
    # written for "timelapse" should cover a timelapse variant added later
    # without anyone remembering to update the rule.
    category: Mapped[str] = mapped_column(String(50), default="analysis", nullable=False, index=True)

    # All relative to settings.derived_dir, so moving the data directory does not
    # invalidate every row. The variants are null when the original is already
    # small enough that deriving one would be pointless.
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    preview_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    web_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    media_type: Mapped[str] = mapped_column(String(100), nullable=False)

    state: Mapped[str] = mapped_column(String(20), default="live", nullable=False)

    # How many times this product has been rebuilt. Only counts rebuilds of a
    # finished product; a live one is rewritten hundreds of times a night and
    # counting that would say nothing.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
