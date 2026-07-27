"""The /api/auth surface.

Every route here is reachable without a session - the middleware treats the whole
prefix as public, because a login page that required being logged in would be a
short story. Protection is therefore declared per route: anything that touches
the account depends on `require_session`, which also enforces CSRF. Read that as
the rule, not a convention - a new route added below without that dependency is
an unauthenticated route.

Responses never contain a password hash, a session token, a CSRF token belonging
to another session, or a confirmed TOTP secret. The one secret that does leave
the server is a *pending* TOTP secret during enrolment, which is unavoidable:
the operator has to be able to type it into their authenticator.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
import structlog

from app.auth import passwords, sessions, setup, throttle, totp
from app.auth.dependencies import require_account, require_session
from app.config import get_settings
from app.db.database import get_db_session
from app.models.admin_account import AdminAccount
from app.models.admin_session import AdminSession
from app.models.trusted_device import TrustedDevice
from app.repositories.admin_account_repository import AdminAccountRepository
from app.security import api_key_required

logger = structlog.get_logger()

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Every wrong credential gets this, whatever was actually wrong with it. Telling
# a caller which half failed hands them a username oracle for free.
INVALID_CREDENTIALS = "Incorrect username, password or code."

# Argon2id verification is slow by design. Running it against a throwaway hash
# when the username does not match keeps a wrong username as slow as a wrong
# password, so response time cannot be used to enumerate the account.
_TIMING_DECOY_HASH = passwords.hash_password("skyhub-timing-decoy")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None

    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


# ---------- request bodies ----------


class SetupRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=3, max_length=150)
    password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        cleaned = value.strip()

        if not cleaned:
            raise ValueError("Username is required.")

        # Printable, no whitespace: a username with a trailing space or a control
        # character is a support ticket waiting to happen.
        if any(character.isspace() for character in cleaned) or not cleaned.isprintable():
            raise ValueError("Username must not contain spaces or control characters.")

        return cleaned


class CodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    remember_device: bool = False


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)


class PasswordConfirmRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=passwords.MAX_PASSWORD_LENGTH)


# ---------- helpers ----------


def enrolment_payload(account: AdminAccount) -> dict:
    """Everything an authenticator app needs, and nothing that outlives enrolment."""
    secret = account.pending_totp_secret

    if not secret:
        raise HTTPException(status_code=409, detail="No enrolment is in progress.")

    uri = totp.provisioning_uri(secret, account.username)

    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_svg": totp.qr_svg(uri),
    }


def guard_throttle(request: Request) -> None:
    """Refuse early when this source address is in backoff."""
    wait = throttle.retry_after(sessions.client_address(request))

    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )


def punish(request: Request, account: AdminAccount | None, repo: AdminAccountRepository) -> None:
    """Record a failure against both the address and the account.

    Only for attempts that named the real account. An attempt against a username
    that does not exist must never reach the account counter - see the login
    route for why.
    """
    throttle.record_failure(sessions.client_address(request))

    if account is not None:
        repo.record_failure(account)


def verify_totp_or_fail(
    account: AdminAccount,
    repo: AdminAccountRepository,
    secret: str | None,
    code: str,
) -> int:
    """Check a code against a secret, enforcing the replay guard."""
    counter = totp.verify(secret or "", code)

    if counter is None:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    if not repo.totp_counter_is_fresh(account, counter):
        # The code is arithmetically correct but its time step has already been
        # spent. Almost always a double-submitted form; occasionally a replay.
        raise HTTPException(status_code=401, detail="That code has already been used.")

    return counter


# ---------- status ----------


@router.get("/status")
async def auth_status(request: Request, db: Session = Depends(get_db_session)) -> dict:
    """What the UI needs to decide which screen to show.

    Public, and carefully thin: whether an account exists, whether this caller is
    signed in, and if so who they are. No secrets, no hint about whether a given
    username exists, no session identifiers.
    """
    repo = AdminAccountRepository(db)
    account = repo.get()
    record = sessions.load_session(db, request.cookies.get(sessions.SESSION_COOKIE))
    authenticated = record is not None and record.stage == sessions.STAGE_ACTIVE

    return {
        "setup_required": account is None,
        "setup_open": setup.is_open(),
        "totp_enrolled": bool(account and account.totp_confirmed),
        "authenticated": authenticated,
        "username": account.username if (authenticated and account) else None,
        "totp_pending": record is not None and record.stage == sessions.STAGE_TOTP_PENDING,
        "api_key_required": api_key_required(),
        "session_idle_minutes": get_settings().session_idle_minutes,
    }


# ---------- first run ----------


@router.post("/setup")
async def create_admin(
    payload: SetupRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> dict:
    """Claim the single admin account, using the token printed at startup."""
    guard_throttle(request)
    repo = AdminAccountRepository(db)

    if repo.exists():
        raise HTTPException(status_code=409, detail="An admin account already exists.")

    if not setup.token_is_valid(payload.setup_token):
        throttle.record_failure(sessions.client_address(request))
        logger.warning("auth.setup.bad_token", client=sessions.client_address(request))
        raise HTTPException(status_code=401, detail="Invalid setup token.")

    try:
        password = passwords.validate_password(payload.password, payload.username)
    except passwords.PasswordPolicyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    account = repo.create(payload.username, password, totp.new_secret())
    throttle.record_success(sessions.client_address(request))

    # A pending session carries the operator from here to the TOTP confirmation
    # without a second trip through the setup token.
    record, token = sessions.create_session(db, account.account_id, request)
    sessions.set_session_cookie(response, request, token, record)

    logger.info("auth.setup.account_created", username=account.username)

    return enrolment_payload(account)


@router.post("/setup/confirm")
async def confirm_admin(
    payload: CodeRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> dict:
    """Prove the authenticator was enrolled, then finish setup and sign in."""
    guard_throttle(request)
    repo = AdminAccountRepository(db)
    account = repo.get()

    if account is None:
        raise HTTPException(status_code=409, detail="No admin account to confirm.")

    if account.totp_confirmed:
        raise HTTPException(status_code=409, detail="Setup is already complete.")

    record = sessions.load_session(db, request.cookies.get(sessions.SESSION_COOKIE))

    if record is None or record.stage != sessions.STAGE_TOTP_PENDING:
        raise HTTPException(status_code=401, detail="Start the setup again.")

    counter = verify_totp_or_fail(account, repo, account.pending_totp_secret, payload.code)

    repo.confirm_totp_secret(account)
    repo.record_totp_counter(account, counter)
    repo.record_success(account)

    promoted, token = sessions.promote_session(db, record, request)
    sessions.set_session_cookie(response, request, token, promoted)

    if payload.remember_device:
        sessions.trust_device(db, account.account_id, request, response)

    # The bootstrap token has done its job; leaving it alive would leave a second
    # way in for as long as the process runs.
    setup.finish()
    throttle.record_success(sessions.client_address(request))

    logger.info("auth.setup.completed", username=account.username)

    return {"status": "authenticated", "username": account.username}


# ---------- login ----------


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> dict:
    """Step one: username and password. Never sufficient on its own."""
    guard_throttle(request)
    repo = AdminAccountRepository(db)
    account = repo.get()

    if account is None:
        raise HTTPException(status_code=409, detail="This server has no admin account yet.")

    locked = repo.lockout_seconds(account)

    if locked > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {locked} seconds.",
            headers={"Retry-After": str(locked)},
        )

    if payload.username.strip().casefold() != account.username.casefold():
        # Burn the same time a real verification costs before failing, so the
        # response time does not distinguish a wrong username from a wrong
        # password.
        passwords.verify_password(_TIMING_DECOY_HASH, payload.password)

        # Only the address is punished here, never the account. Counting a wrong
        # username toward the account lockout would let anyone who can reach the
        # port lock the operator out without knowing a single real credential.
        throttle.record_failure(sessions.client_address(request))
        logger.warning("auth.login.unknown_user", client=sessions.client_address(request))

        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    if not repo.verify_password(account, payload.password):
        punish(request, account, repo)
        logger.warning("auth.login.bad_password", client=sessions.client_address(request))
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    # The password was right, so the address is no longer suspect. The second
    # factor has its own guard below and does not need the streak carried over.
    throttle.record_success(sessions.client_address(request))

    # An account that never finished enrolling has no second factor to check.
    # Send it back to enrolment rather than into a prompt nothing can answer.
    if not account.totp_confirmed:
        record, token = sessions.create_session(db, account.account_id, request)
        sessions.set_session_cookie(response, request, token, record)

        if not account.pending_totp_secret:
            repo.stage_totp_secret(account, totp.new_secret())

        return {"status": "totp_enrolment_required", **enrolment_payload(account)}

    if sessions.device_is_trusted(db, account.account_id, request):
        # This browser has already proved control of the second factor and the
        # password has just been checked again, which is what makes skipping the
        # code legitimate rather than a bypass.
        record, token = sessions.create_session(
            db, account.account_id, request, stage=sessions.STAGE_ACTIVE
        )
        sessions.set_session_cookie(response, request, token, record)
        repo.record_success(account)

        logger.info("auth.login.trusted_device", username=account.username)

        return {"status": "authenticated", "username": account.username}

    record, token = sessions.create_session(db, account.account_id, request)
    sessions.set_session_cookie(response, request, token, record)

    return {"status": "totp_required"}


@router.post("/totp")
async def submit_totp(
    payload: CodeRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> dict:
    """Step two: the six digit code, against the pending session from step one."""
    guard_throttle(request)
    repo = AdminAccountRepository(db)
    account = repo.get()
    record = sessions.load_session(db, request.cookies.get(sessions.SESSION_COOKIE))

    if account is None or record is None or record.stage != sessions.STAGE_TOTP_PENDING:
        raise HTTPException(status_code=401, detail="Start signing in again.")

    locked = repo.lockout_seconds(account)

    if locked > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {locked} seconds.",
            headers={"Retry-After": str(locked)},
        )

    # Enrolment that was interrupted after the password step lands here with a
    # pending secret and nothing confirmed yet.
    enrolling = not account.totp_confirmed
    secret = account.pending_totp_secret if enrolling else account.totp_secret

    try:
        counter = verify_totp_or_fail(account, repo, secret, payload.code)
    except HTTPException:
        punish(request, account, repo)
        logger.warning("auth.login.bad_totp", client=sessions.client_address(request))
        raise

    if enrolling:
        repo.confirm_totp_secret(account)
        setup.finish()

    repo.record_totp_counter(account, counter)
    repo.record_success(account)
    throttle.record_success(sessions.client_address(request))

    promoted, token = sessions.promote_session(db, record, request)
    sessions.set_session_cookie(response, request, token, promoted)

    if payload.remember_device:
        sessions.trust_device(db, account.account_id, request, response)

    logger.info("auth.login.success", username=account.username)

    return {"status": "authenticated", "username": account.username}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> dict:
    """Invalidate this session immediately, server side, then clear the cookies.

    Deliberately tolerant: a caller with no session, an expired one or a
    half-finished login all get the same 200 and a cleared cookie jar. Logging
    out is not an operation anyone should have to be authorised for, and a 401
    here would strand a browser holding a cookie it cannot get rid of.
    """
    record = sessions.load_session(db, request.cookies.get(sessions.SESSION_COOKIE))

    if record is not None:
        sessions.revoke_session(db, record)

    sessions.clear_session_cookie(response, request)

    return {"status": "signed_out"}


# ---------- account management (session only, never an API key) ----------


@router.get("/sessions")
async def list_sessions(
    request: Request,
    session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Live sessions for this account.

    The identifier shown is a prefix of the stored fingerprint - enough to tell
    two rows apart and to revoke by, and useless as a credential even in full.
    """
    records = sessions.list_sessions(db, session.account_id)

    return {
        "sessions": [
            {
                "id": record.session_id[:12],
                "current": record.session_id == session.session_id,
                "created_at": isoformat(record.created_at),
                "last_seen_at": isoformat(record.last_seen_at),
                "expires_at": isoformat(record.expires_at),
                "absolute_expires_at": isoformat(record.absolute_expires_at),
                "ip_address": record.ip_address or None,
                "user_agent": record.user_agent or None,
            }
            for record in records
        ]
    }


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Sign every other browser out, keeping this one."""
    removed = sessions.revoke_all(db, session.account_id, keep_session_id=session.session_id)

    logger.info("auth.sessions.revoked_others", count=removed)

    return {"revoked": removed}


@router.post("/password")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    session: AdminSession = Depends(require_session),
    account: AdminAccount = Depends(require_account),
    db: Session = Depends(get_db_session),
) -> dict:
    """Change the password, proving the current one first.

    Every other session is dropped afterwards. A password change is what someone
    does when they think a credential has leaked, and leaving other sessions
    alive would make it useless against exactly that.
    """
    guard_throttle(request)
    repo = AdminAccountRepository(db)

    if not repo.verify_password(account, payload.current_password):
        throttle.record_failure(sessions.client_address(request))
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    try:
        new_password = passwords.validate_password(payload.new_password, account.username)
    except passwords.PasswordPolicyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if repo.verify_password(account, new_password):
        raise HTTPException(status_code=422, detail="The new password matches the old one.")

    repo.set_password(account, new_password)
    throttle.record_success(sessions.client_address(request))

    removed = sessions.revoke_all(db, account.account_id, keep_session_id=session.session_id)

    logger.info("auth.password.changed", other_sessions_revoked=removed)

    return {"status": "password_changed", "other_sessions_revoked": removed}


@router.post("/totp/reset")
async def reset_totp(
    payload: PasswordConfirmRequest,
    request: Request,
    account: AdminAccount = Depends(require_account),
    _session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Stage a new TOTP secret. The current one keeps working until it is confirmed.

    Staging rather than replacing is what stops a mistyped re-enrolment from
    locking the account out of its own second factor: nothing changes until a
    code from the new secret is proved.
    """
    guard_throttle(request)
    repo = AdminAccountRepository(db)

    if not repo.verify_password(account, payload.current_password):
        throttle.record_failure(sessions.client_address(request))
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    throttle.record_success(sessions.client_address(request))
    repo.stage_totp_secret(account, totp.new_secret())

    logger.info("auth.totp.reset_started")

    return enrolment_payload(account)


@router.post("/totp/reset/confirm")
async def confirm_totp_reset(
    payload: CodeRequest,
    request: Request,
    response: Response,
    account: AdminAccount = Depends(require_account),
    session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Activate the staged secret, then invalidate everything the old one blessed."""
    guard_throttle(request)
    repo = AdminAccountRepository(db)

    if not account.pending_totp_secret:
        raise HTTPException(status_code=409, detail="No enrolment is in progress.")

    try:
        counter = verify_totp_or_fail(account, repo, account.pending_totp_secret, payload.code)
    except HTTPException:
        throttle.record_failure(sessions.client_address(request))
        raise

    repo.confirm_totp_secret(account)
    repo.record_totp_counter(account, counter)
    throttle.record_success(sessions.client_address(request))

    # Both of these were authorised by the secret that was just retired.
    forgotten = sessions.forget_devices(db, account.account_id)
    removed = sessions.revoke_all(db, account.account_id, keep_session_id=session.session_id)
    sessions.clear_trusted_device_cookie(response, request)

    logger.info("auth.totp.reset_completed", devices_forgotten=forgotten, sessions_revoked=removed)

    return {
        "status": "totp_updated",
        "trusted_devices_forgotten": forgotten,
        "other_sessions_revoked": removed,
    }


@router.post("/devices/forget")
async def forget_trusted_devices(
    request: Request,
    response: Response,
    account: AdminAccount = Depends(require_account),
    _session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Make every remembered browser ask for a TOTP code again."""
    forgotten = sessions.forget_devices(db, account.account_id)
    sessions.clear_trusted_device_cookie(response, request)

    return {"trusted_devices_forgotten": forgotten}


@router.get("/devices")
async def list_trusted_devices(
    account: AdminAccount = Depends(require_account),
    _session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> dict:
    """Browsers currently allowed to skip the TOTP prompt."""
    now = utc_now()
    records = (
        db.query(TrustedDevice)
        .filter(TrustedDevice.account_id == account.account_id, TrustedDevice.expires_at > now)
        .order_by(TrustedDevice.created_at.desc())
        .all()
    )

    return {
        "devices": [
            {
                "id": record.device_id[:12],
                "created_at": isoformat(record.created_at),
                "last_used_at": isoformat(record.last_used_at),
                "expires_at": isoformat(record.expires_at),
                "user_agent": record.user_agent or None,
            }
            for record in records
        ]
    }
