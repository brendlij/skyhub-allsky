from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.auth import passwords
from app.models.admin_account import AdminAccount

ACCOUNT_ID = "admin"

# Lockout on the account itself, independent of the per-IP backoff. This is the
# ceiling on guessing the one password that exists, and it survives a restart.
LOCKOUT_THRESHOLD = 10
LOCKOUT_MINUTES = 15

# A streak only counts while it is a streak. Ten wrong guesses spread over a
# month is someone with a bad memory, not an attack.
STREAK_WINDOW_MINUTES = 15


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class AdminAccountRepository:
    """Every read and write of the single admin row goes through here."""

    def __init__(self, db: Session):
        self.db = db

    def get(self) -> AdminAccount | None:
        return self.db.get(AdminAccount, ACCOUNT_ID)

    def exists(self) -> bool:
        return self.get() is not None

    def create(self, username: str, password: str, totp_secret: str) -> AdminAccount:
        """Create the one account. Refuses to overwrite an existing one.

        That refusal is the guard on the whole bootstrap: even if the setup route
        were somehow reachable after configuration, it could not replace the
        operator's credentials.
        """
        if self.exists():
            raise ValueError("An admin account already exists.")

        now = utc_now()
        account = AdminAccount(
            account_id=ACCOUNT_ID,
            username=username,
            password_hash=passwords.hash_password(password),
            pending_totp_secret=totp_secret,
            totp_secret=None,
            totp_confirmed=False,
            created_at=now,
            password_changed_at=now,
        )

        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)

        return account

    # ---------- passwords ----------

    def set_password(self, account: AdminAccount, password: str) -> AdminAccount:
        account.password_hash = passwords.hash_password(password)
        account.password_changed_at = utc_now()

        self.db.commit()
        self.db.refresh(account)

        return account

    def verify_password(self, account: AdminAccount, password: str) -> bool:
        """Check a password and transparently upgrade an old hash on success."""
        if not passwords.verify_password(account.password_hash, password):
            return False

        if passwords.needs_rehash(account.password_hash):
            account.password_hash = passwords.hash_password(password)
            self.db.commit()

        return True

    # ---------- lockout ----------

    def lockout_seconds(self, account: AdminAccount) -> int:
        """How long the account is barred for, 0 when it is not."""
        locked_until = _as_utc(account.locked_until)

        if locked_until is None:
            return 0

        remaining = (locked_until - utc_now()).total_seconds()

        return max(0, int(remaining))

    def record_failure(self, account: AdminAccount) -> None:
        now = utc_now()
        last_failed = _as_utc(account.last_failed_at)

        if last_failed is None or last_failed + timedelta(minutes=STREAK_WINDOW_MINUTES) < now:
            account.failed_attempts = 0

        account.failed_attempts += 1
        account.last_failed_at = now

        if account.failed_attempts >= LOCKOUT_THRESHOLD:
            account.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            account.failed_attempts = 0

        self.db.commit()

    def record_success(self, account: AdminAccount) -> None:
        account.failed_attempts = 0
        account.locked_until = None
        account.last_failed_at = None
        account.last_login_at = utc_now()

        self.db.commit()

    # ---------- TOTP ----------

    def stage_totp_secret(self, account: AdminAccount, secret: str) -> AdminAccount:
        """Park a new secret without disturbing the one currently in use."""
        account.pending_totp_secret = secret

        self.db.commit()
        self.db.refresh(account)

        return account

    def confirm_totp_secret(self, account: AdminAccount) -> AdminAccount:
        """Promote the staged secret once the operator has proved they enrolled it."""
        account.totp_secret = account.pending_totp_secret
        account.pending_totp_secret = None
        account.totp_confirmed = True
        # Belongs to the previous secret; keeping it could reject a legitimate
        # first code from the new one.
        account.last_totp_counter = None

        self.db.commit()
        self.db.refresh(account)

        return account

    def discard_pending_totp(self, account: AdminAccount) -> None:
        account.pending_totp_secret = None
        self.db.commit()

    def record_totp_counter(self, account: AdminAccount, counter: int) -> None:
        account.last_totp_counter = counter
        self.db.commit()

    def totp_counter_is_fresh(self, account: AdminAccount, counter: int) -> bool:
        """Reject a code from a step already used - the RFC 6238 replay guard."""
        if account.last_totp_counter is None:
            return True

        return counter > account.last_totp_counter
