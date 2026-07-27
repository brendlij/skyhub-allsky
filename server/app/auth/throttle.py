"""Per-client backoff for failed logins.

Two layers, because they stop different attacks:

  * The account lockout (persisted on the admin row) bounds guessing against the
    one password that matters, and survives a restart.
  * This module bounds guessing per source address, so a botnet cannot use the
    single shared account as a free pass by spreading attempts, and so one
    attacker cannot lock the real operator out by failing on purpose - the
    operator's own address stays unthrottled.

In-memory on purpose. The state is worth seconds, not durability, and a table
written on every failed request is a denial of service vector of its own. A
restart clears the per-IP counters; the persisted account lockout is what makes
that safe.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading

from app.config import get_settings

# Bounded so a flood of forged source addresses cannot grow this without limit.
# Well past the number of distinct clients any real SkyHub sees.
MAX_TRACKED_CLIENTS = 2048


@dataclass
class Attempt:
    failures: int = 0
    blocked_until: datetime | None = None
    last_failure_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_attempts: dict[str, Attempt] = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_seconds(failures: int) -> int:
    """Exponential, from the configured base, capped.

    The first failure is free - a typo should not cost a wait - and every one
    after that doubles: 2s, 4s, 8s ... up to the ceiling.
    """
    settings = get_settings()

    if failures < 2:
        return 0

    delay = settings.login_backoff_base_seconds * (2 ** (failures - 2))

    return int(min(delay, settings.login_backoff_max_seconds))


def _prune(now: datetime) -> None:
    """Drop entries that have gone quiet, and hard-cap the table."""
    window = timedelta(minutes=get_settings().login_attempt_window_minutes)
    stale = [
        key for key, attempt in _attempts.items()
        if attempt.last_failure_at + window < now
        and (attempt.blocked_until is None or attempt.blocked_until < now)
    ]

    for key in stale:
        del _attempts[key]

    if len(_attempts) <= MAX_TRACKED_CLIENTS:
        return

    # Still over the cap after pruning: evict the oldest, which is the least
    # likely to be an attack in progress.
    for key, _ in sorted(_attempts.items(), key=lambda item: item[1].last_failure_at)[
        : len(_attempts) - MAX_TRACKED_CLIENTS
    ]:
        del _attempts[key]


def retry_after(client: str) -> int:
    """Seconds this client must wait, 0 when it may try now."""
    if not client:
        return 0

    now = _now()

    with _lock:
        attempt = _attempts.get(client)

        if attempt is None or attempt.blocked_until is None:
            return 0

        if attempt.blocked_until <= now:
            return 0

        return max(1, int((attempt.blocked_until - now).total_seconds()))


def record_failure(client: str) -> int:
    """Count a failed attempt, returning the wait it just earned."""
    if not client:
        return 0

    now = _now()
    window = timedelta(minutes=get_settings().login_attempt_window_minutes)

    with _lock:
        _prune(now)
        attempt = _attempts.get(client)

        # A quiet spell resets the streak: this throttles a run of guesses, not
        # someone who mistypes once a week.
        if attempt is None or attempt.last_failure_at + window < now:
            attempt = Attempt()
            _attempts[client] = attempt

        attempt.failures += 1
        attempt.last_failure_at = now

        delay = backoff_seconds(attempt.failures)
        attempt.blocked_until = now + timedelta(seconds=delay) if delay else None

        return delay


def record_success(client: str) -> None:
    """Clear a client's streak after a login that worked."""
    if not client:
        return

    with _lock:
        _attempts.pop(client, None)


def reset() -> None:
    """Test hook: forget every tracked client."""
    with _lock:
        _attempts.clear()
