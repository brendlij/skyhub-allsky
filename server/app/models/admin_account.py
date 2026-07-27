from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AdminAccount(Base):
    """The single human operator of this server.

    One row, always keyed "admin" - SkyHub has one account by design, and a fixed
    primary key makes "is this server configured yet" a single get() rather than a
    count that a second row could quietly break.

    Only ever holds derived secrets: an Argon2id hash that cannot be reversed into
    the password, and the TOTP shared secret, which by construction has to be
    stored in a recoverable form. Neither is exposed through any API response.
    """

    __tablename__ = "admin_account"

    account_id: Mapped[str] = mapped_column(String(50), primary_key=True, default="admin")

    username: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Pending secret lives beside the active one during a reset, so a half
    # finished re-enrolment cannot lock the account out of its own second factor.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pending_totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # RFC 6238 replay guard: a code stays valid for its whole step, so without
    # this one shoulder-surfed code can be used twice inside the same 30 seconds.
    last_totp_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Persisted rather than in-memory so a restart cannot clear a lockout, which
    # would otherwise make the backoff trivial to defeat.
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
