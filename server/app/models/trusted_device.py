from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TrustedDevice(Base):
    """A browser allowed to skip the TOTP prompt - and only the TOTP prompt.

    The password is still demanded on every login. This cookie shortens the second
    factor for a machine the operator has already proved control of, which is the
    only thing "remember this browser" may ever mean; a cookie that skipped the
    password would be a password equivalent sitting in a cookie jar.

    Same storage rule as a session: the row holds a SHA-256 of a 256-bit token, so
    the database never contains anything replayable.
    """

    __tablename__ = "trusted_devices"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
