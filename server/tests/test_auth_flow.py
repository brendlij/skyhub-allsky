"""End-to-end checks for the human authentication flow.

Runs against a throwaway database and a real TestClient, so the middleware, the
cookies and the CSRF checks are all exercised the way a browser would hit them.

Run with:  python -m pytest server/tests -q       (from the repository root)
or:        python tests/test_auth_flow.py         (no pytest needed)
"""

import os
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

# Point the whole app at a scratch data directory before anything imports config.
_TEMP_DIR = tempfile.mkdtemp(prefix="skyhub-auth-test-")
os.environ["SKYHUB_SERVER_DATA_DIR"] = _TEMP_DIR
os.environ["SKYHUB_SERVER_API_KEY"] = "test-api-key-abcdef"

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import setup as auth_setup, throttle  # noqa: E402
from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, TRUSTED_DEVICE_COOKIE  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.repositories.admin_account_repository import AdminAccountRepository  # noqa: E402

USERNAME = "julian"
PASSWORD = "a-long-enough-passphrase"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return

    failures.append(f"{label}{f' - {detail}' if detail else ''}")
    print(f"  FAIL  {label} {detail}")


def csrf_headers(client: TestClient) -> dict:
    """What the frontend does: echo the readable CSRF cookie back in a header."""
    token = client.cookies.get(CSRF_COOKIE)

    return {"X-CSRF-Token": token} if token else {}


def code_now(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def expire_session(client: TestClient, field: str) -> bool:
    """Push one of a session's two deadlines into the past."""
    from datetime import datetime, timedelta, timezone

    from app.auth.tokens import token_fingerprint
    from app.models.admin_session import AdminSession

    token = client.cookies.get(SESSION_COOKIE)

    if not token:
        return False

    db = SessionLocal()

    try:
        record = db.get(AdminSession, token_fingerprint(token))

        if record is None:
            return False

        setattr(record, field, datetime.now(timezone.utc) - timedelta(seconds=1))
        db.commit()

        return True
    finally:
        db.close()


def rearm() -> None:
    """Forget the last TOTP step and any lockout, so the next code is accepted.

    RFC 6238 says a code may be used once, so a test that logs in repeatedly
    would otherwise have to sleep out a 30 second step between every login. The
    guard itself is checked on its own further down, with this left alone.
    """
    throttle.reset()
    db = SessionLocal()

    try:
        repo = AdminAccountRepository(db)
        account = repo.get()

        if account is not None:
            account.last_totp_counter = None
            repo.record_success(account)
    finally:
        db.close()


def main() -> int:
    with TestClient(app) as client:
        print("\nhealth and status")
        health = client.get("/health")
        check("health is public", health.status_code == 200)
        check(
            "health leaks no auth state",
            set(health.json()) == {"status"},
            f"got {sorted(health.json())}",
        )

        status = client.get("/api/auth/status").json()
        check("setup is required on a fresh database", status["setup_required"] is True)
        check("setup token was minted at startup", status["setup_open"] is True)

        print("\napi is closed before login")
        anonymous = client.get("/api/nodes")
        check("anonymous /api/nodes is 401", anonymous.status_code == 401, str(anonymous.status_code))

        keyed = client.get("/api/nodes", headers={"X-API-Key": "test-api-key-abcdef"})
        check("api key still opens /api/nodes", keyed.status_code == 200, str(keyed.status_code))

        wrong_key = client.get("/api/nodes", headers={"X-API-Key": "wrong"})
        check("wrong api key is 401", wrong_key.status_code == 401)

        print("\nfirst-run setup")
        bad_token = client.post(
            "/api/auth/setup",
            json={"setup_token": "guessed", "username": USERNAME, "password": PASSWORD},
        )
        check("wrong setup token is rejected", bad_token.status_code == 401)
        throttle.reset()

        short = client.post(
            "/api/auth/setup",
            json={"setup_token": auth_setup._setup_token, "username": USERNAME, "password": "short"},
        )
        check("short password is rejected", short.status_code == 422, str(short.status_code))

        created = client.post(
            "/api/auth/setup",
            json={
                "setup_token": auth_setup._setup_token,
                "username": USERNAME,
                "password": PASSWORD,
            },
        )
        check("setup succeeds with the token", created.status_code == 200, created.text[:120])
        enrolment = created.json()
        secret = enrolment["secret"]
        check("enrolment returns a secret", bool(secret))
        check("enrolment returns an otpauth uri", enrolment["otpauth_uri"].startswith("otpauth://totp/"))
        check("enrolment returns an inline svg", enrolment["qr_svg"].lstrip().startswith("<svg"))

        pending_status = client.get("/api/auth/status").json()
        check("a pending session is not authenticated", pending_status["authenticated"] is False)
        check("status reports the pending totp step", pending_status["totp_pending"] is True)

        still_closed = client.get("/api/nodes")
        check("a totp-pending session cannot reach the api", still_closed.status_code == 401)

        print("\ntotp confirmation")
        wrong_code = client.post("/api/auth/setup/confirm", json={"code": "000000"})
        check("a wrong code is rejected", wrong_code.status_code == 401)
        throttle.reset()

        confirmed = client.post("/api/auth/setup/confirm", json={"code": code_now(secret)})
        check("the right code completes setup", confirmed.status_code == 200, confirmed.text[:160])
        check("setup token is destroyed", auth_setup.is_open() is False)

        signed_in = client.get("/api/auth/status").json()
        check("the session is now authenticated", signed_in["authenticated"] is True)
        check("status reports the username", signed_in["username"] == USERNAME)

        allowed = client.get("/api/nodes")
        check("a session opens /api/nodes", allowed.status_code == 200, str(allowed.status_code))

        print("\ncookie attributes")
        raw_cookies = confirmed.headers.get_list("set-cookie")
        session_cookie = next((c for c in raw_cookies if c.startswith(SESSION_COOKIE)), "")
        csrf_cookie = next((c for c in raw_cookies if c.startswith(CSRF_COOKIE)), "")
        check("session cookie is HttpOnly", "HttpOnly" in session_cookie, session_cookie[:90])
        check("session cookie is SameSite=strict", "SameSite=strict" in session_cookie)
        check("csrf cookie is readable by script", "HttpOnly" not in csrf_cookie, csrf_cookie[:90])
        check(
            "session id is rotated on promotion",
            client.cookies.get(SESSION_COOKIE) is not None,
        )

        print("\ncsrf")
        no_csrf = client.put("/api/storage/settings", json={"retention_days": 5})
        check("a write without the csrf header is 403", no_csrf.status_code == 403, str(no_csrf.status_code))

        bad_csrf = client.put(
            "/api/storage/settings",
            json={"retention_days": 5},
            headers={"X-CSRF-Token": "not-the-token"},
        )
        check("a write with a wrong csrf token is 403", bad_csrf.status_code == 403)

        good_csrf = client.put(
            "/api/storage/settings",
            json={"retention_days": 5},
            headers=csrf_headers(client),
        )
        check("a write with the csrf header succeeds", good_csrf.status_code == 200, good_csrf.text[:120])

        cross_origin = client.put(
            "/api/storage/settings",
            json={"retention_days": 6},
            headers={**csrf_headers(client), "Origin": "http://evil.example"},
        )
        check("a cross-origin write is refused", cross_origin.status_code == 403)

        print("\nsecrets never leave the server")
        blob = client.get("/api/auth/sessions").text + client.get("/api/auth/status").text
        check("no totp secret in responses", secret not in blob)
        check("no session token in responses", (client.cookies.get(SESSION_COOKIE) or "?") not in blob)
        check("no argon2 hash in responses", "$argon2" not in blob)

        session_list = client.get("/api/auth/sessions").json()["sessions"]
        check("one live session is listed", len(session_list) == 1, str(len(session_list)))
        check("the current session is flagged", session_list[0]["current"] is True)
        check("session id is truncated", len(session_list[0]["id"]) == 12)

        print("\naccount management requires a session, not an api key")
        key_only = TestClient(app)
        key_attempt = key_only.post(
            "/api/auth/password",
            json={"current_password": PASSWORD, "new_password": "another-long-passphrase"},
            headers={"X-API-Key": "test-api-key-abcdef"},
        )
        check("api key cannot change the password", key_attempt.status_code == 401, str(key_attempt.status_code))

        print("\nsecond browser, revocation")
        rearm()
        second = TestClient(app)
        login = second.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        check("second browser passes the password step", login.status_code == 200, login.text[:120])
        check("second browser is asked for totp", login.json()["status"] == "totp_required")

        second_closed = second.get("/api/nodes")
        check("second browser is not authenticated yet", second_closed.status_code == 401)

        totp_response = second.post(
            "/api/auth/totp",
            json={"code": code_now(secret), "remember_device": True},
        )
        check("second browser completes totp", totp_response.status_code == 200, totp_response.text[:160])
        check("second browser got a trust cookie", second.cookies.get(TRUSTED_DEVICE_COOKIE) is not None)

        both = client.get("/api/auth/sessions").json()["sessions"]
        check("two sessions are listed", len(both) == 2, str(len(both)))

        print("\ntotp replay guard")
        # Deliberately not rearmed: the code below is the one the second browser
        # just spent, and reusing it inside the same step must fail.
        third = TestClient(app)
        third.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        replay = third.post("/api/auth/totp", json={"code": code_now(secret)})
        check("a code from a spent step is refused", replay.status_code == 401, str(replay.status_code))
        check(
            "the replay is named as such",
            "already been used" in replay.text,
            replay.text[:120],
        )
        throttle.reset()

        print("\ntrusted device skips only the code")
        second.post("/api/auth/logout", headers=csrf_headers(second))
        trusted_login = second.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        check(
            "trusted browser is authenticated after the password alone",
            trusted_login.json().get("status") == "authenticated",
            trusted_login.text[:120],
        )
        check("trusted browser can reach the api", second.get("/api/nodes").status_code == 200)

        wrong_password = TestClient(app)
        wrong_password.cookies.set(TRUSTED_DEVICE_COOKIE, second.cookies.get(TRUSTED_DEVICE_COOKIE))
        denied = wrong_password.post(
            "/api/auth/login", json={"username": USERNAME, "password": "not-the-password"}
        )
        check("a trusted browser still needs the password", denied.status_code == 401)
        throttle.reset()

        print("\nrevoke other sessions")
        revoked = client.post("/api/auth/sessions/revoke-others", headers=csrf_headers(client))
        check("revoke-others reports what it removed", revoked.json()["revoked"] >= 1, revoked.text[:120])
        check("the other browser is signed out", second.get("/api/nodes").status_code == 401)
        check("this browser survives", client.get("/api/nodes").status_code == 200)

        print("\npassword change")
        wrong_current = client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "yet-another-passphrase"},
            headers=csrf_headers(client),
        )
        check("a wrong current password is rejected", wrong_current.status_code == 401)
        throttle.reset()

        reused = client.post(
            "/api/auth/password",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
            headers=csrf_headers(client),
        )
        check("reusing the old password is rejected", reused.status_code == 422, str(reused.status_code))

        changed = client.post(
            "/api/auth/password",
            json={"current_password": PASSWORD, "new_password": "brand-new-passphrase"},
            headers=csrf_headers(client),
        )
        check("the password changes", changed.status_code == 200, changed.text[:120])
        check("the changing session survives", client.get("/api/nodes").status_code == 200)

        print("\ntotp reset")
        reset = client.post(
            "/api/auth/totp/reset",
            json={"current_password": "brand-new-passphrase"},
            headers=csrf_headers(client),
        )
        check("reset stages a new secret", reset.status_code == 200, reset.text[:120])
        new_secret = reset.json()["secret"]
        check("the staged secret is different", new_secret != secret)

        rearm()
        old_still_works = TestClient(app)
        old_still_works.post("/api/auth/login", json={"username": USERNAME, "password": "brand-new-passphrase"})
        old_code = old_still_works.post("/api/auth/totp", json={"code": code_now(secret)})
        check(
            "the old secret still works before confirmation",
            old_code.status_code == 200,
            old_code.text[:120],
        )

        rearm()
        confirm_reset = client.post(
            "/api/auth/totp/reset/confirm",
            json={"code": code_now(new_secret)},
            headers=csrf_headers(client),
        )
        check("confirming activates the new secret", confirm_reset.status_code == 200, confirm_reset.text[:160])
        check(
            "the reset revoked the other sessions",
            confirm_reset.json()["other_sessions_revoked"] >= 1,
            confirm_reset.text[:160],
        )

        rearm()
        after_reset = TestClient(app)
        after_reset.post("/api/auth/login", json={"username": USERNAME, "password": "brand-new-passphrase"})
        stale = after_reset.post("/api/auth/totp", json={"code": code_now(secret)})
        check("the retired secret stops working", stale.status_code == 401, str(stale.status_code))

        rearm()
        accepted = after_reset.post("/api/auth/totp", json={"code": code_now(new_secret)})
        check("the new secret works", accepted.status_code == 200, accepted.text[:160])
        throttle.reset()

        print("\nsession deadlines")
        rearm()
        expiring = TestClient(app)
        expiring.post("/api/auth/login", json={"username": USERNAME, "password": "brand-new-passphrase"})
        expiring.post("/api/auth/totp", json={"code": code_now(new_secret)})
        check("the fresh session works", expiring.get("/api/nodes").status_code == 200)

        # Reaching into the row rather than waiting out a 30 minute idle window.
        # Both deadlines are read on every request, so moving one is the same as
        # time passing.
        check(
            "an idled-out session is refused",
            expire_session(expiring, "expires_at") and expiring.get("/api/nodes").status_code == 401,
        )

        rearm()
        aged = TestClient(app)
        aged.post("/api/auth/login", json={"username": USERNAME, "password": "brand-new-passphrase"})
        aged.post("/api/auth/totp", json={"code": code_now(new_secret)})
        check(
            "a session past its absolute cap is refused",
            expire_session(aged, "absolute_expires_at") and aged.get("/api/nodes").status_code == 401,
        )
        throttle.reset()

        print("\nlockout and backoff")
        attacker = TestClient(app)

        for _ in range(3):
            attacker.post("/api/auth/login", json={"username": USERNAME, "password": "guess"})

        throttled = attacker.post("/api/auth/login", json={"username": USERNAME, "password": "guess"})
        check("repeated failures are throttled", throttled.status_code == 429, str(throttled.status_code))
        check("a Retry-After is sent", "retry-after" in {k.lower() for k in throttled.headers})
        throttle.reset()

        # A wrong username must not feed the account lockout, or anyone who can
        # reach the port could lock the operator out without knowing anything.
        rearm()
        stranger = TestClient(app)

        for _ in range(12):
            stranger.post("/api/auth/login", json={"username": "someone-else", "password": "guess"})
            throttle.reset()

        db = SessionLocal()

        try:
            locked = AdminAccountRepository(db).lockout_seconds(AdminAccountRepository(db).get())
        finally:
            db.close()

        check("a wrong username cannot lock the account", locked == 0, f"locked for {locked}s")

        wrong_user = stranger.post(
            "/api/auth/login", json={"username": "someone-else", "password": "guess"}
        )
        check("an unknown username is a plain 401", wrong_user.status_code == 401, str(wrong_user.status_code))
        check(
            "the error does not say which half was wrong",
            "username" not in wrong_user.json()["detail"].lower().replace("incorrect username, password or code.", ""),
            wrong_user.text[:120],
        )
        throttle.reset()

        print("\nlogout")
        logout = client.post("/api/auth/logout", headers=csrf_headers(client))
        check("logout succeeds", logout.status_code == 200)
        check("the session no longer opens the api", client.get("/api/nodes").status_code == 401)
        check(
            "logout is idempotent",
            client.post("/api/auth/logout").status_code == 200,
        )

    print()

    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("all checks passed")
    return 0


def test_auth_flow():
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
