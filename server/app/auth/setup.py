"""First-run bootstrap.

The hard problem with a first-run wizard is the window before anyone has claimed
the account: whoever reaches the page first becomes the admin. On a LAN that is
usually fine, and it is what most self-hosted software does - but "usually fine"
is not "secure by default", and the requirement here is the latter.

So the wizard demands a token that only someone with access to the server itself
can read. It is printed to the startup log and written to `data/setup-token.txt`
with owner-only permissions, then destroyed the moment setup completes. Nobody
who merely reached the web port can finish the wizard.

The token lives in memory as well as on disk: the file is a convenience for the
operator, and the in-memory copy is what is actually compared, so tampering with
the file cannot lower the bar.
"""

from pathlib import Path
import os
import secrets

import structlog

from app.auth.tokens import tokens_match
from app.config import get_settings

logger = structlog.get_logger()

TOKEN_FILENAME = "setup-token.txt"

_setup_token: str | None = None


def token_path() -> Path:
    return get_settings().data_dir / TOKEN_FILENAME


def begin(reason: str = "no admin account") -> str:
    """Mint the bootstrap token and make it available to the operator."""
    global _setup_token

    # A token per server start: a stale one left in a log from last week should
    # not still open the wizard.
    _setup_token = secrets.token_urlsafe(24)
    path = token_path()

    try:
        path.write_text(_setup_token + "\n", encoding="utf-8")

        # Owner only. Best effort - Windows ignores the mode, and a failure here
        # must not stop the server from starting.
        os.chmod(path, 0o600)
    except OSError as error:
        logger.warning("auth.setup.token_file_failed", error=str(error), path=str(path))

    logger.warning(
        "auth.setup.required",
        reason=reason,
        setup_token=_setup_token,
        token_file=str(path),
        detail=(
            "SkyHub has no admin account yet. Open the web UI and enter this "
            "one-time setup token to create one. It is regenerated on every "
            "restart and deleted once setup completes."
        ),
    )

    return _setup_token


def finish() -> None:
    """Invalidate the token in memory and on disk."""
    global _setup_token

    _setup_token = None

    try:
        token_path().unlink(missing_ok=True)
    except OSError as error:
        logger.warning("auth.setup.token_cleanup_failed", error=str(error))


def is_open() -> bool:
    return _setup_token is not None


def token_is_valid(candidate: str | None) -> bool:
    """Constant-time check against the in-memory token."""
    if _setup_token is None:
        return False

    return tokens_match(str(candidate or "").strip(), _setup_token)
