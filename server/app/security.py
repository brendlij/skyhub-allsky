"""API key authentication.

Off by default: an allsky server with no key set behaves exactly as before, which
keeps a LAN-only install working after an upgrade. Set SKYHUB_SERVER_API_KEY and
every /api route and both WebSockets start demanding it.

The key is accepted three ways because the clients differ in what they can send:

    X-API-Key: <key>            scripts, Home Assistant, the web UI
    Authorization: Bearer <key> OpenAPI clients that only speak bearer tokens
    ?api_key=<key>              <img> tags and browser WebSockets, neither of
                                which can set a header

The query parameter is the weak one - it ends up in server logs and browser
history - so prefer a header wherever the client allows it.
"""

import secrets

from fastapi import HTTPException, Request, WebSocket
import structlog

from app.config import get_settings

logger = structlog.get_logger()

API_KEY_HEADER = "X-API-Key"
API_KEY_QUERY = "api_key"

# Open to everyone even when a key is set: a monitoring probe should not need a
# credential, and it reveals nothing but that the server is up.
PUBLIC_PATHS = frozenset({"/health"})

# Routes that public capture access may unlock. Deliberately only the newest frame:
# whoever gets in this way can see the current sky, not browse the archive, read
# telemetry or change anything. Their ?raw= and ?thumb= variants come along, since
# an un-overlaid sky is no more sensitive than an overlaid one.
PUBLIC_CAPTURE_PATHS = frozenset({"/api/captures/current", "/api/captures/latest"})


def api_key_required() -> bool:
    return bool(get_settings().api_key)


def presented_key(headers, query_params) -> str | None:
    header_key = headers.get(API_KEY_HEADER)

    if header_key:
        return header_key

    authorization = headers.get("Authorization") or ""

    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return query_params.get(API_KEY_QUERY)


def key_is_valid(candidate: str | None) -> bool:
    expected = get_settings().api_key

    if not expected:
        return True

    if not candidate:
        return False

    # Constant time: a plain == leaks the key one character at a time to anyone
    # who can measure the response.
    return secrets.compare_digest(str(candidate), str(expected))


def capture_access_is_public(path: str, candidate: str | None) -> bool:
    """Whether this request may see the current image without the full API key."""
    if path not in PUBLIC_CAPTURE_PATHS:
        return False

    settings = get_settings()

    if settings.public_captures:
        return True

    if not settings.public_capture_token:
        return False

    return bool(candidate) and secrets.compare_digest(
        str(candidate), str(settings.public_capture_token)
    )


def request_is_authorised(request: Request) -> bool:
    if not api_key_required() or request.url.path in PUBLIC_PATHS:
        return True

    candidate = presented_key(request.headers, request.query_params)

    if key_is_valid(candidate):
        return True

    # The read-only token is offered the same way as the API key, so a client only
    # ever has to know about one parameter - it just unlocks far less.
    return capture_access_is_public(request.url.path, candidate)


async def websocket_is_authorised(websocket: WebSocket) -> bool:
    """Check a WebSocket handshake, closing it when the key is missing or wrong.

    The close has to happen before accept(), otherwise the client sees a working
    connection that then goes quiet.
    """
    if not api_key_required():
        return True

    if key_is_valid(presented_key(websocket.headers, websocket.query_params)):
        return True

    logger.warning("websocket.unauthorised", path=websocket.url.path)
    await websocket.close(code=1008, reason="Invalid or missing API key")

    return False


def require_api_key(request: Request) -> None:
    """Dependency form, for routes that want the check declared explicitly."""
    if not request_is_authorised(request):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def log_startup_state() -> None:
    settings = get_settings()

    if api_key_required():
        logger.info(
            "api.auth.enabled",
            public_captures=settings.public_captures,
            public_capture_token=bool(settings.public_capture_token),
        )

        if settings.public_captures:
            logger.info(
                "api.public_captures.open",
                paths=sorted(PUBLIC_CAPTURE_PATHS),
                detail="The newest capture is readable without any credential.",
            )

        return

    logger.warning(
        "api.auth.disabled",
        detail=(
            "No SKYHUB_SERVER_API_KEY set: every API route and both WebSockets are "
            "open to anyone who can reach this port. Fine on a trusted LAN, not "
            "for anything reachable from the internet."
        ),
    )
