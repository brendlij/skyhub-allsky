"""Opaque token generation and the one-way form stored in the database."""

import hashlib
import hmac
import secrets

# 256 bits. Long enough that the stored SHA-256 has no dictionary to attack, which
# is what lets these be hashed with a fast algorithm instead of Argon2.
TOKEN_BYTES = 32


def new_token() -> str:
    """A URL-safe secret. Handed to the client once and never stored as-is."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """The database form of a token: irreversible, fixed width, fast to look up."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(candidate: str | None, expected: str | None) -> bool:
    """Constant time equality for secrets that are compared, not looked up."""
    if not candidate or not expected:
        return False

    return hmac.compare_digest(str(candidate), str(expected))
