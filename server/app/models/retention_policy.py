from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RetentionPolicy(Base):
    """How long a category of derived product is allowed to accumulate.

    Scoped either globally or to one node, with the node's rule winning where it
    exists. Two independent limits, because they answer different questions:
    `keep_days` is "how far back do I care", `max_gb` is "how much disk can this
    have". Whichever bites first applies.

    Null means unlimited for both, which is the default for every category - an
    upgrade must not start deleting an operator's startrails because a default
    they never chose said ninety days.
    """

    __tablename__ = "retention_policies"

    # "global:timelapse" or "pi5-hqcam:timelapse". Composite as a single string
    # so the row is a plain get() rather than a two-column lookup.
    policy_id: Mapped[str] = mapped_column(String(160), primary_key=True)

    scope: Mapped[str] = mapped_column(String(100), default="global", nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    keep_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_gb: Mapped[float | None] = mapped_column(Float, nullable=True)

    # How many rebuilds of the same product to keep. Only meaningful once a
    # product has been re-finalised; 1 means "only the current one".
    keep_versions: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
