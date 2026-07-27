# Authentication

SkyHub authenticates two kinds of caller, and keeps them apart on purpose.

| | Humans | Machines |
|---|---|---|
| Who | you, in a browser | camera nodes, Home Assistant, cron |
| Credential | username + password + TOTP | shared API key |
| Carried as | HttpOnly session cookie | `X-API-Key` header |
| Can manage the account | yes | **never** |
| Revocable | yes, per session | only by changing the key |

A camera node cannot answer a two-factor prompt, so it has to hold a plain string.
That is exactly why the string is never enough to take the account over: password
changes, TOTP re-enrolment and session management require a signed-in browser, and
return `401` to any API key.

---

## First run

The server starts, finds no account, and prints a one-time setup token:

```
auth.setup.required  reason='no admin account'
                     setup_token=WA-_fwzE3KPaCbFXc1xcgxc417yMHNbw
                     token_file=/path/to/data/setup-token.txt
```

The token gates the setup wizard. Without it, whoever reaches the web port first
becomes the admin - which is how a lot of self-hosted software works, and is not
what "secure by default" means. Requiring it means only someone who can read the
server's log or filesystem can claim the account.

1. Open the UI. It lands on the setup wizard.
2. Paste the token, choose a username and a password of at least 12 characters.
3. Scan the QR code with your authenticator app.
4. Enter the six-digit code it shows.

The token is regenerated on every restart and destroyed - in memory and on disk -
the moment setup completes.

**Save the secret** shown under "Can't scan? Show the key" somewhere safe. Losing
the authenticator without it means the recovery procedure at the bottom of this
page.

---

## Signing in

1. Username and password. Verified with Argon2id.
2. The six-digit TOTP code.

Between the two steps the browser holds a *pending* session: it exists, but it
authenticates nothing. Only `/api/auth/totp` accepts it, and it expires after five
minutes. When the code checks out, the session identifier is **rotated** - the
token that ends up authenticated is one nobody could have planted beforehand,
which is the standard defence against session fixation.

### Remember this browser

Ticking "remember this browser for 30 days" skips **the TOTP prompt only**. The
password is demanded on every login, every time. A cookie that skipped the password
would be a password sitting in a cookie jar.

The cookie is an opaque 256-bit token; the server stores only its SHA-256. Replacing
the authenticator forgets every remembered browser.

---

## Sessions

| Property | Value | Why |
|---|---|---|
| Storage | server-side row | so it can be revoked; a JWT cannot |
| Cookie | `skyhub_session` | HttpOnly, SameSite=Strict, Secure over HTTPS |
| Idle timeout | 30 minutes | catches an abandoned browser |
| Absolute lifetime | 24 hours | caps a stolen cookie regardless of activity |

`HttpOnly` means script cannot read the token, so an XSS bug can act as you but
cannot walk off with your session - the thing localStorage cannot offer, and the
reason there are no JWTs here. `Secure` is set when the request arrived over HTTPS
and omitted otherwise, so a plain-HTTP LAN install still works.

Both deadlines are checked on every request. The idle one slides forward on
activity; the absolute one never moves.

Tune them with `SKYHUB_SERVER_SESSION_IDLE_MINUTES` and
`SKYHUB_SERVER_SESSION_ABSOLUTE_HOURS`.

---

## CSRF

Every `POST`, `PUT`, `PATCH` and `DELETE` made with a session cookie needs an
`X-CSRF-Token` header matching the token stored on that session. `GET` never does.

Two layers, deliberately:

1. **SameSite=Strict** - the browser refuses to attach the session cookie to a
   cross-site request at all. This is the primary defence.
2. **Double submit** - the token is compared against the *session row*, not merely
   against a cookie of the same name, so someone who can write cookies onto your
   domain still cannot make both halves agree.

Plus an `Origin` check on state-changing requests, applied only when the browser
sent one - curl and camera nodes send none.

The frontend reads the token from the deliberately non-HttpOnly `skyhub_csrf`
cookie and echoes it back. That cookie is safe for script to read because on its
own it authenticates nothing.

---

## Rate limiting

Two independent layers:

**Per source address** (in memory). The first failure is free; every one after that
doubles the wait - 2s, 4s, 8s … capped at 300s. A quiet spell of 15 minutes resets
the streak, so a mistyped password once a week costs nothing.

**Per account** (persisted). Ten consecutive failures locks the account for 15
minutes. It is stored on the account row, so restarting the server does not clear
it - otherwise the backoff would be trivial to defeat.

The account layer bounds guessing against the one password that exists. The address
layer stops a botnet from spreading attempts across many sources, and stops one
attacker locking you out of your own camera by failing on purpose - your address
stays unthrottled.

Configure with `SKYHUB_SERVER_LOGIN_BACKOFF_BASE_SECONDS`,
`SKYHUB_SERVER_LOGIN_BACKOFF_MAX_SECONDS` and
`SKYHUB_SERVER_LOGIN_ATTEMPT_WINDOW_MINUTES`.

---

## What is stored

| Secret | Stored as | Ever returned by the API |
|---|---|---|
| Password | Argon2id hash (m=19456, t=2, p=1) | no |
| Session token | SHA-256 of a 256-bit random token | no |
| Trusted device token | SHA-256 of a 256-bit random token | no |
| CSRF token | plaintext, scoped to one session | only its own session's, in a cookie |
| TOTP secret | plaintext - unavoidable, it is a shared secret | only while enrolling |

Argon2id parameters follow the OWASP Password Storage Cheat Sheet baseline rather
than argon2-cffi's heavier defaults, because a SkyHub server is routinely a
Raspberry Pi and 64 MiB of hashing per login is seconds of stall on one. The
parameters live in the hash string, so raising them later upgrades each hash the
next time that password is used.

Session tokens are hashed with SHA-256 rather than Argon2 deliberately: they are
256 bits of CSPRNG output, so there is no dictionary to run against them the way
there is for a password.

The session list in Settings → Security shows a 12-character prefix of the stored
fingerprint - enough to tell two rows apart, useless as a credential even in full.

---

## TOTP

RFC 6238, SHA-1, 6 digits, 30-second step, one step of drift either way. Those are
the RFC defaults and the only combination every authenticator app reads reliably -
Google Authenticator silently ignores the algorithm and digits parameters in an
`otpauth://` URI, so a server that picked SHA-256 would hand out codes that never
match. The strength is the 160-bit secret.

A code may be used **once**. The time step of each accepted code is recorded and
anything not strictly newer is refused, per RFC 6238 §5.2 - so a code read over
your shoulder cannot be replayed inside the same half-minute. Signing in to a
second device within the same 30 seconds means waiting for the next code. That is
correct behaviour, not a bug.

### Replacing the authenticator

Settings → Security → Replace authenticator. Confirm your password, scan the new
QR, enter a code from it.

The new secret is *staged*: the old authenticator keeps working until a code from
the new one is proved, so a mistyped re-enrolment cannot lock you out. Confirming
retires the old secret, forgets every remembered browser and signs out every other
session - all of which were blessed by the secret being retired.

---

## API keys, and what they no longer do

The shared key still authenticates camera nodes and automation:

```bash
curl -H "X-API-Key: $KEY"            http://skyhub.local:8000/api/nodes
curl -H "Authorization: Bearer $KEY" http://skyhub.local:8000/api/nodes
curl "http://skyhub.local:8000/api/nodes?api_key=$KEY"
```

It no longer goes anywhere near a browser. The web UI holds a session cookie, which
the browser attaches to image requests and the WebSocket handshake on its own - so
the key has stopped appearing in `<img>` URLs, server logs and browser history.

By default the key also opens the rest of `/api`, which is what existing scripts
and Home Assistant installs depend on. To make the two systems fully disjoint:

```bash
SKYHUB_SERVER_API_KEY_NODES_ONLY=true
```

The key then opens only `/api/captures/upload` and the node WebSocket. Anything
else needs a session. This breaks automation that reads `/api/nodes`, which is why
it is not the default.

Either way, the key never opens `/api/auth/password`, `/api/auth/totp/reset`,
`/api/auth/sessions` or `/api/auth/devices`.

---

## Endpoints

Public - no credential:

| Method | Path | |
|---|---|---|
| GET | `/health` | liveness only; deliberately reveals no auth state |
| GET | `/api/auth/status` | which screen the UI should show |
| POST | `/api/auth/setup` | first run, needs the setup token |
| POST | `/api/auth/setup/confirm` | first run, needs the pending session |
| POST | `/api/auth/login` | password step |
| POST | `/api/auth/totp` | code step, needs the pending session |
| POST | `/api/auth/logout` | tolerant: always 200, always clears cookies |

Session required - an API key gets `401`:

| Method | Path | |
|---|---|---|
| GET | `/api/auth/sessions` | live sessions |
| POST | `/api/auth/sessions/revoke-others` | sign every other browser out |
| POST | `/api/auth/password` | needs the current password |
| POST | `/api/auth/totp/reset` | needs the current password |
| POST | `/api/auth/totp/reset/confirm` | needs a code from the new secret |
| GET | `/api/auth/devices` | remembered browsers |
| POST | `/api/auth/devices/forget` | forget them all |

Everything else under `/api` takes either a session or an API key, subject to
`SKYHUB_SERVER_API_KEY_NODES_ONLY`.

---

## Behind a reverse proxy

If SkyHub sits behind nginx, Caddy or Traefik terminating TLS:

```bash
SKYHUB_SERVER_TRUST_PROXY_HEADERS=true
```

This makes the server believe `X-Forwarded-Proto` (so the `Secure` cookie flag is
set correctly) and `X-Forwarded-For` (so rate limiting sees real client addresses
rather than the proxy). Leave it off otherwise: an unvalidated `X-Forwarded-For`
lets anyone dodge per-address throttling by inventing a new address per request.

Make sure the proxy passes `Host` through unchanged, or the `Origin` check will
reject state-changing requests.

---

## Recovery

**Lost authenticator, but you saved the secret.** Re-enter it in a new app.

**Lost authenticator and secret.** Stop the server and delete the account row, then
restart - the server sees no account and prints a fresh setup token:

```bash
sqlite3 data/skyhub.db "DELETE FROM admin_account; DELETE FROM admin_sessions; DELETE FROM trusted_devices;"
```

Captures, nodes and settings are untouched. Physical access to the database is
inherently full access; this is not a bypass, it is what "the server is the root of
trust" means.

**Locked out by the backoff.** Wait it out, or restart the server and clear the
lockout:

```bash
sqlite3 data/skyhub.db "UPDATE admin_account SET locked_until = NULL, failed_attempts = 0;"
```

---

## Testing

`server/tests/test_auth_flow.py` exercises the whole flow against a real
`TestClient` and a scratch database - setup, login, CSRF, session rotation, the
replay guard, lockout, revocation and logout:

```bash
python server/tests/test_auth_flow.py
```
