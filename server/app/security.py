"""Request authorisation: which credential opens which door.

SkyHub has two kinds of caller and they are authenticated differently on purpose.

**Humans** use the web UI and hold a session cookie, obtained with a username, an
Argon2id-verified password and a TOTP code. See `app.auth`. A browser is a hostile
place to keep a long-lived secret, so it keeps none: the cookie is an opaque
lookup token, HttpOnly, and the server can revoke it.

**Machines** - camera nodes, Home Assistant, cron jobs - use the shared API key.
A node cannot answer a TOTP prompt, so it must have a credential that is simply a
string. The key is therefore never accepted for anything that could take the
account over: password changes, TOTP re-enrolment and session management all
require a session (`app.auth.dependencies.require_session`), never a key.

How far the key reaches otherwise is the operator's call:

    api_key_nodes_only = False   (default) the key also opens the rest of /api,
                                 which is what every existing script and Home
                                 Assistant install already depends on.
    api_key_nodes_only = True    the key opens only the node routes. Human and
                                 machine access become fully disjoint, at the
                                 cost of any automation that reads /api/nodes.

The key is accepted three ways, because clients differ in what they can send:

    X-API-Key: <key>            scripts, Home Assistant, camera nodes
    Authorization: Bearer <key> OpenAPI clients that only speak bearer tokens
    ?api_key=<key>              <img> tags and browser WebSockets, neither of
                                which can set a header

The query parameter is the weak one - it ends up in server logs and browser
history - so prefer a header wherever the client allows it. A logged-in browser
needs none of them: the cookie rides along on image requests by itself.
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

# The login surface itself, which by definition cannot require being logged in.
# Each route does its own throttling and validation; see app.auth.routes.
PUBLIC_AUTH_PREFIX = "/api/auth/"

# Routes that public capture access may unlock. Deliberately only the newest frame:
# whoever gets in this way can see the current sky, not browse the archive, read
# telemetry or change anything. Their ?raw= and ?thumb= variants come along, since
# an un-overlaid sky is no more sensitive than an overlaid one.
PUBLIC_CAPTURE_PATHS = frozenset({"/api/captures/current", "/api/captures/latest"})

# What a camera node needs to do its job, and nothing more. This is the whole
# surface an API key still reaches when api_key_nodes_only is on.
NODE_PATHS = frozenset({"/api/captures/upload"})


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
        return False

    if not candidate:
        return False

    # Constant time: a plain == leaks the key one character at a time to anyone
    # who can measure the response.
    return secrets.compare_digest(str(candidate), str(expected))


def path_is_node_route(path: str) -> bool:
    return path in NODE_PATHS


def api_key_opens(path: str) -> bool:
    """Whether a valid API key is accepted for this path."""
    if not get_settings().api_key_nodes_only:
        return True

    return path_is_node_route(path)


def capture_access_is_public(path: str, candidate: str | None) -> bool:
    """Whether this request may see the current image without a full credential."""
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


def request_has_valid_key(request: Request) -> bool:
    """A machine credential good for this particular path."""
    if not api_key_required():
        return False

    candidate = presented_key(request.headers, request.query_params)

    return key_is_valid(candidate) and api_key_opens(request.url.path)


def request_is_public(request: Request) -> bool:
    """Paths that never need a credential of any kind."""
    path = request.url.path

    if path in PUBLIC_PATHS:
        return True

    if path.startswith(PUBLIC_AUTH_PREFIX):
        return True

    candidate = presented_key(request.headers, request.query_params)

    return capture_access_is_public(path, candidate)


async def websocket_key_is_valid(websocket: WebSocket) -> bool:
    """Whether a WebSocket handshake carries a usable API key.

    Node sockets are the reason this exists: a camera node has no cookie jar.
    """
    if not api_key_required():
        return False

    return key_is_valid(presented_key(websocket.headers, websocket.query_params))


def require_api_key(request: Request) -> None:
    """Dependency form, for node routes that want the check declared explicitly."""
    if request_has_valid_key(request):
        return

    if not api_key_required():
        return

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def log_startup_state() -> None:
    settings = get_settings()

    if api_key_required():
        logger.info(
            "api.auth.key_enabled",
            nodes_only=settings.api_key_nodes_only,
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
        "api.auth.key_disabled",
        detail=(
            "No SKYHUB_SERVER_API_KEY set. The web UI still requires a login, but "
            "camera nodes and automation connect unauthenticated - anyone who can "
            "reach this port can upload captures. Set a key before exposing it."
        ),
    )
