from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class AdminSession(Base):
    """A server-side session. The cookie carries a lookup token and nothing else.

    Stored as a SHA-256 of the token, so a leaked database - a backup, a stolen
    disk - cannot be replayed as a live session. A fast hash is the right choice
    here and not a shortcut: the token is 256 bits of CSPRNG output, so there is
    no dictionary to run against it the way there is for a password.

    Two independent deadlines, per OWASP: `expires_at` slides forward on activity
    and catches an abandoned browser, `absolute_expires_at` never moves and caps
    how long a single stolen cookie can be useful.
    """

    __tablename__ = "admin_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)

    # "totp_pending" holds a caller that has passed the password but not yet the
    # second factor. It authenticates nothing: only the TOTP route accepts it.
    stage: Mapped[str] = mapped_column(String(20), default="totp_pending", nullable=False)

    # Paired with the double-submit cookie. Random per session and rotated with
    # the session id, so it cannot outlive the session it belongs to.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Shown in the session list so an unfamiliar entry is recognisable. Truncated
    # on write - a user agent string is attacker-controlled input.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
