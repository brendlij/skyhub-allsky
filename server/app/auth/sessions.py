"""Session lifecycle, cookies, CSRF and trusted devices.

Sessions live in the database and the browser holds nothing but a lookup token.
That is the whole reason not to use a JWT here: a stateless token cannot be
revoked, and "log out everywhere" and "revoke that session I do not recognise"
are both explicit requirements. A row can simply be deleted.

Cookie rules, all three of them load-bearing:

    HttpOnly          script cannot read the token, so an XSS bug cannot exfiltrate
                      the session the way it can read localStorage
    SameSite=Strict   the browser refuses to attach the cookie to cross-site
                      requests at all, which is the primary CSRF defence
    Secure            set when the request arrived over HTTPS, so the cookie
                      never travels in clear - and not set on plain HTTP, or a
                      LAN install would silently fail to log in at all
"""

from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from sqlalchemy.orm import Session

from app.auth.tokens import new_token, token_fingerprint, tokens_match
from app.config import get_settings
from app.models.admin_session import AdminSession
from app.models.trusted_device import TrustedDevice

SESSION_COOKIE = "skyhub_session"
CSRF_COOKIE = "skyhub_csrf"
TRUSTED_DEVICE_COOKIE = "skyhub_device"
CSRF_HEADER = "X-CSRF-Token"

STAGE_TOTP_PENDING = "totp_pending"
STAGE_ACTIVE = "active"

# A half-finished login is not a session. Long enough to fish a phone out of a
# pocket, short enough that an abandoned password prompt is not left standing.
TOTP_PENDING_MINUTES = 5

# Methods that change something. A CSRF token is demanded on all of them, and on
# nothing else, so a plain read never fails for want of a header.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; compare them as UTC, not local time."""
    if value is None:
        return None

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def request_is_secure(request: Request) -> bool:
    """Whether this request reached us over TLS, directly or via a trusted proxy."""
    if request.url.scheme == "https":
        return True

    if get_settings().trust_proxy_headers:
        return request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"

    return False


def client_address(request: Request) -> str:
    """Best available source address, used for throttling and the session list.

    A forwarded header is only believed when the operator has said there is a
    proxy in front. Otherwise anyone could send a fresh X-Forwarded-For per
    request and never be throttled twice.
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")

        if forwarded:
            return forwarded.split(",")[0].strip()[:64]

    return (request.client.host if request.client else "")[:64]


def user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:255]


# ---------- session records ----------


def create_session(
    db: Session,
    account_id: str,
    request: Request,
    stage: str = STAGE_TOTP_PENDING,
) -> tuple[AdminSession, str]:
    """Open a session, returning the row and the token the cookie will carry.

    The token is returned once and never again: only its fingerprint is stored.
    """
    settings = get_settings()
    token = new_token()
    now = utc_now()

    lifetime = (
        timedelta(minutes=TOTP_PENDING_MINUTES)
        if stage == STAGE_TOTP_PENDING
        else timedelta(minutes=settings.session_idle_minutes)
    )

    record = AdminSession(
        session_id=token_fingerprint(token),
        account_id=account_id,
        stage=stage,
        csrf_token=new_token(),
        created_at=now,
        last_seen_at=now,
        expires_at=now + lifetime,
        absolute_expires_at=now + timedelta(hours=settings.session_absolute_hours),
        ip_address=client_address(request),
        user_agent=user_agent(request),
    )

    db.add(record)
    db.commit()
    db.refresh(record)

    return record, token


def promote_session(db: Session, record: AdminSession, request: Request) -> tuple[AdminSession, str]:
    """Turn a TOTP-pending session into a real one, with a brand new token.

    Rotating the identifier at the moment privilege is granted is the fix for
    session fixation: a token an attacker planted before login is discarded here,
    so the one that ends up authenticated is one only this browser has seen. The
    CSRF token is regenerated with it - it is scoped to the session, and letting
    it outlive the rotation would leave a valid token bound to a dead row.
    """
    settings = get_settings()
    now = utc_now()

    db.delete(record)
    db.flush()

    token = new_token()
    promoted = AdminSession(
        session_id=token_fingerprint(token),
        account_id=record.account_id,
        stage=STAGE_ACTIVE,
        csrf_token=new_token(),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.session_idle_minutes),
        # Carried over, not restarted: the absolute cap counts from when the
        # login began, or a refresh at the TOTP step would extend it for free.
        absolute_expires_at=_as_utc(record.absolute_expires_at) or (
            now + timedelta(hours=settings.session_absolute_hours)
        ),
        ip_address=client_address(request),
        user_agent=user_agent(request),
    )

    db.add(promoted)
    db.commit()
    db.refresh(promoted)

    return promoted, token


def session_is_live(record: AdminSession | None, now: datetime | None = None) -> bool:
    """Whether this row is still usable: not revoked, not idle out, not aged out."""
    if record is None:
        return False

    moment = now or utc_now()

    if record.revoked_at is not None:
        return False

    if (expires_at := _as_utc(record.expires_at)) and expires_at <= moment:
        return False

    if (absolute := _as_utc(record.absolute_expires_at)) and absolute <= moment:
        return False

    return True


def load_session(db: Session, token: str | None) -> AdminSession | None:
    """Resolve a cookie token to a live session row, or None."""
    if not token:
        return None

    record = db.get(AdminSession, token_fingerprint(token))

    return record if session_is_live(record) else None


def touch_session(db: Session, record: AdminSession) -> None:
    """Slide the idle deadline forward. The absolute deadline never moves."""
    settings = get_settings()
    now = utc_now()

    record.last_seen_at = now
    record.expires_at = now + timedelta(minutes=settings.session_idle_minutes)

    db.commit()


def revoke_session(db: Session, record: AdminSession) -> None:
    """Delete rather than flag: a revoked session has no reason to be kept."""
    db.delete(record)
    db.commit()


def revoke_all(db: Session, account_id: str, keep_session_id: str | None = None) -> int:
    """Drop every session for the account, optionally sparing the current one."""
    query = db.query(AdminSession).filter(AdminSession.account_id == account_id)

    if keep_session_id:
        query = query.filter(AdminSession.session_id != keep_session_id)

    removed = query.delete(synchronize_session=False)
    db.commit()

    return removed


def list_sessions(db: Session, account_id: str) -> list[AdminSession]:
    """Live, fully authenticated sessions, newest first.

    Half-finished logins are left out - they authenticate nothing, and showing
    them would only invite someone to hunt for a session that is not there.
    """
    now = utc_now()
    records = (
        db.query(AdminSession)
        .filter(AdminSession.account_id == account_id, AdminSession.stage == STAGE_ACTIVE)
        .order_by(AdminSession.created_at.desc())
        .all()
    )

    return [record for record in records if session_is_live(record, now)]


def purge_expired(db: Session) -> int:
    """Housekeeping: clear rows both deadlines have passed."""
    now = utc_now()
    removed = (
        db.query(AdminSession)
        .filter(AdminSession.absolute_expires_at <= now)
        .delete(synchronize_session=False)
    )
    removed += (
        db.query(TrustedDevice)
        .filter(TrustedDevice.expires_at <= now)
        .delete(synchronize_session=False)
    )
    db.commit()

    return removed


# ---------- CSRF ----------


def csrf_is_valid(request: Request, record: AdminSession) -> bool:
    """Double submit: the header must match the token stored on the session.

    SameSite=Strict already stops a cross-site form from carrying the session
    cookie, so this is the second layer rather than the only one - it still holds
    if a browser mishandles SameSite, or if a future route relaxes it.

    The value is compared against the session row, not merely against the cookie
    of the same name, so an attacker who can write cookies onto the victim's
    domain still cannot make both halves agree.
    """
    presented = request.headers.get(CSRF_HEADER)

    return tokens_match(presented, record.csrf_token)


# ---------- cookies ----------


def _cookie_kwargs(request: Request, max_age: int) -> dict:
    return {
        "max_age": max_age,
        "path": "/",
        "httponly": True,
        "secure": request_is_secure(request),
        "samesite": "strict",
    }


def set_session_cookie(response: Response, request: Request, token: str, record: AdminSession) -> None:
    """Attach the session token, plus the CSRF token the page needs to read.

    The CSRF cookie is deliberately *not* HttpOnly: the frontend has to echo it
    back in a header, which is the entire mechanism. It is safe for script to
    read because on its own it authenticates nothing - it is only ever checked
    alongside a session cookie that script cannot touch.
    """
    settings = get_settings()
    max_age = (
        TOTP_PENDING_MINUTES * 60
        if record.stage == STAGE_TOTP_PENDING
        else settings.session_absolute_hours * 3600
    )

    response.set_cookie(SESSION_COOKIE, token, **_cookie_kwargs(request, max_age))
    response.set_cookie(
        CSRF_COOKIE,
        record.csrf_token,
        **{**_cookie_kwargs(request, max_age), "httponly": False},
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Expire both cookies. Attributes must match the originals or Set-Cookie is ignored."""
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            secure=request_is_secure(request),
            samesite="strict",
        )


# ---------- trusted devices ----------


def trust_device(db: Session, account_id: str, request: Request, response: Response) -> None:
    """Remember this browser so it is not asked for TOTP again for 30 days."""
    settings = get_settings()
    token = new_token()
    now = utc_now()
    max_age = settings.trusted_device_days * 86400

    db.add(
        TrustedDevice(
            device_id=token_fingerprint(token),
            account_id=account_id,
            created_at=now,
            expires_at=now + timedelta(days=settings.trusted_device_days),
            last_used_at=now,
            user_agent=user_agent(request),
        )
    )
    db.commit()

    response.set_cookie(TRUSTED_DEVICE_COOKIE, token, **_cookie_kwargs(request, max_age))


def device_is_trusted(db: Session, account_id: str, request: Request) -> bool:
    """Whether this browser may skip TOTP - never whether it may skip the password."""
    token = request.cookies.get(TRUSTED_DEVICE_COOKIE)

    if not token:
        return False

    record = db.get(TrustedDevice, token_fingerprint(token))

    if record is None or record.account_id != account_id:
        return False

    if (expires_at := _as_utc(record.expires_at)) and expires_at <= utc_now():
        db.delete(record)
        db.commit()
        return False

    record.last_used_at = utc_now()
    db.commit()

    return True


def forget_devices(db: Session, account_id: str) -> int:
    """Drop every remembered browser. Used whenever the second factor changes."""
    removed = (
        db.query(TrustedDevice)
        .filter(TrustedDevice.account_id == account_id)
        .delete(synchronize_session=False)
    )
    db.commit()

    return removed


def clear_trusted_device_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        TRUSTED_DEVICE_COOKIE,
        path="/",
        secure=request_is_secure(request),
        samesite="strict",
    )
