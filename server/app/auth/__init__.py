"""Human authentication for the SkyHub web UI.

Deliberately separate from `app.security`, which authenticates *machines* with the
shared API key. The two never mix: a node cannot answer a TOTP prompt, and a
browser should never be asked to hold a full-control key in localStorage.

Module map, smallest to largest:

    passwords.py   Argon2id hashing and password policy
    totp.py        RFC 6238 codes, enrolment URI, QR rendering
    tokens.py      CSPRNG token generation and the storage hash
    throttle.py    per-IP failed attempt backoff
    sessions.py    session lifecycle, cookies, CSRF, trusted devices
    setup.py       first-run bootstrap token
    dependencies.py FastAPI dependencies for routes that need a human
    routes.py      the /api/auth/* surface
"""
