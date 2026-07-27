from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ProcessingSession(Base):
    """One node's run of captures for one archive date and period.

    A session is what makes the pipeline incremental: processors accumulate into
    it frame by frame, and the heavy encoding happens once when it closes. The row
    exists so that state survives a restart - the in-memory stack is a cache of
    what is on disk under `processing_state_dir`, and this row says which sessions
    are still open and where they got to.

    Keyed by node, date and period rather than by the node's own sequence id: a
    sequence can be stopped and restarted mid-night, and that should extend the
    night's startrail rather than start a second one.
    """

    __tablename__ = "processing_sessions"

    session_key: Mapped[str] = mapped_column(String(200), primary_key=True)

    node_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    archive_date: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # What this session is: "day", "night", "manual", "test", or anything a
    # caller invents. Still named `period` because that is what it is for the two
    # the sun produces, and every processor and route already reads it.
    period: Mapped[str] = mapped_column(String(20), nullable=False)

    # How the session is driven. "solar" opens and closes with sunrise and
    # sunset; anything else is opened and closed by explicit request, so the
    # period watcher leaves it alone.
    session_kind: Mapped[str] = mapped_column(String(30), default="solar", nullable=False)

    # Free text for the sessions the sun did not open - "Perseids", "focus test".
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # "open" while frames are still arriving, "closing" while the encoders run,
    # "closed" when everything finished, "failed" when finalisation gave up.
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)

    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Per-processor bookkeeping: {"startrail": {"frames": 412, "error": null}, …}.
    # Kept as JSON so a new processor needs no migration - the whole point of the
    # registry is that adding one touches nothing outside its own module.
    processor_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Last reported progress per processor, so the UI can show what is happening
    # during a long encode. Persisted rather than memory-only so a page loaded
    # mid-finalisation shows the truth instead of an empty bar.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
