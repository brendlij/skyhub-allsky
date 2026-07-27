"""FastAPI dependencies for routes that require a human, not a machine.

`require_session` is the strict one: it accepts a fully authenticated session
cookie and nothing else. Account management - changing the password, re-enrolling
the second factor, revoking sessions - hangs off it, so an API key can never be
used to take the account over. That is what keeps the two credential systems
genuinely separate rather than merely parallel.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import sessions
from app.db.database import get_db_session
from app.models.admin_account import AdminAccount
from app.models.admin_session import AdminSession
from app.repositories.admin_account_repository import AdminAccountRepository


def current_session(
    request: Request,
    db: Session = Depends(get_db_session),
) -> AdminSession | None:
    """The live, fully authenticated session on this request, if there is one."""
    record = sessions.load_session(db, request.cookies.get(sessions.SESSION_COOKIE))

    if record is None or record.stage != sessions.STAGE_ACTIVE:
        return None

    return record


def require_session(
    request: Request,
    db: Session = Depends(get_db_session),
) -> AdminSession:
    """Demand a logged-in human. 401 for anything else."""
    record = current_session(request, db)

    if record is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    # CSRF is enforced here as well as in the middleware. The middleware is the
    # blanket rule; this makes the guarantee local to the route, so an account
    # route stays protected even if the middleware is ever narrowed.
    if request.method in sessions.UNSAFE_METHODS and not sessions.csrf_is_valid(request, record):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")

    return record


def require_account(
    session: AdminSession = Depends(require_session),
    db: Session = Depends(get_db_session),
) -> AdminAccount:
    """The admin behind the current session."""
    account = AdminAccountRepository(db).get()

    if account is None:
        # The account was deleted underneath a live session. Nothing it could
        # authorise still exists.
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    return account
