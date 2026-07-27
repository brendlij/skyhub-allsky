"""TOTP enrolment and verification (RFC 6238), plus the enrolment QR code.

SHA-1, 6 digits, 30-second step. Those are RFC 6238's defaults and, more to the
point, the only combination every authenticator app reads reliably - Google
Authenticator in particular silently ignores the algorithm and digits parameters
in an otpauth:// URI, so a server that chose SHA-256 would hand out codes that
never match. This is not the place to be clever; the strength here is the secret,
which is 160 bits from the system CSPRNG.

Verification is delegated to pyotp rather than hand-rolled: an HMAC comparison
written locally is exactly the kind of "obvious" code that ends up non-constant
time.
"""

import io
import re
import time

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

DIGITS = 6
INTERVAL_SECONDS = 30

# One step either side, so a phone clock drifting by a few seconds still works.
# Wider than this starts meaningfully extending the window an observed code is
# usable in, which the counter replay guard would then be the only defence for.
VALID_WINDOW = 1

ISSUER = "SkyHub"


def new_secret() -> str:
    """A fresh base32 shared secret - 160 bits, the RFC 4226 recommendation."""
    return pyotp.random_base32()


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(secret, digits=DIGITS, interval=INTERVAL_SECONDS)


def normalise_code(code: str) -> str:
    """Strip the spaces authenticator apps display between digit groups."""
    return "".join(character for character in str(code or "") if character.isdigit())


def code_is_wellformed(code: str) -> bool:
    return len(normalise_code(code)) == DIGITS


def verify(secret: str, code: str) -> int | None:
    """Validate a code, returning the time step it belongs to.

    The step is what makes replay detectable: a code stays valid for its whole
    interval plus the drift window, so one read over a shoulder is otherwise
    reusable for up to a minute. Callers record the returned counter and refuse
    anything that is not strictly newer.

    None means the code is wrong, malformed, or the secret is unset.
    """
    if not secret or not code_is_wellformed(code):
        return None

    totp = _totp(secret)
    normalised = normalise_code(code)
    now = int(time.time())
    match: int | None = None

    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        at = now + offset * INTERVAL_SECONDS

        # No early break: every candidate step is compared with pyotp's constant
        # time helper and the loop always runs to the end, so neither the outcome
        # nor which step matched is observable in the response time.
        if pyotp.utils.strings_equal(totp.at(at), normalised):
            match = at // INTERVAL_SECONDS

    return match


def provisioning_uri(secret: str, username: str) -> str:
    """The otpauth:// URI an authenticator app scans or accepts as text."""
    return _totp(secret).provisioning_uri(name=username or "admin", issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """Render the enrolment URI as an SVG fragment for inlining in the page.

    SVG rather than a PNG because it needs no Pillow round-trip, stays sharp at
    any size, and drops straight into the document - the browser never fetches
    the secret from a second URL, where it would land in access logs and the
    HTTP cache.

    The XML prolog is dropped and the millimetre dimensions replaced by a plain
    viewBox: inside HTML the prolog is invalid and fixed physical sizes ignore
    the layout, while the viewBox alone lets CSS size it.
    """
    image = qrcode.make(uri, image_factory=SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)

    markup = buffer.getvalue().decode("utf-8")

    if markup.startswith("<?xml"):
        markup = markup.split("?>", 1)[1].lstrip()

    return re.sub(r'\s(width|height)="[^"]*"', "", markup, count=2)
