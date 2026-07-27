"""Progress tracking, so the UI can show what a long job is doing.

Two problems this solves. The obvious one: an encode that takes four minutes looks
identical to a hang unless something reports a number. The less obvious one: the
report has to be cheap enough to call from inside a per-frame hook without
becoming the expensive part of it.

So updates are held in memory and only written to the database when they say
something new - a stage change, or a percentage that moved by more than a
threshold. A processor can call `report` on every frame and every ffmpeg progress
line without thinking about it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any, Callable

import structlog

from app.processing.base import STAGE_IDLE

logger = structlog.get_logger()

# Below this, a percentage change is not worth a database write or a WebSocket
# frame - nobody can see a bar move by half a percent.
PERCENT_EPSILON = 2.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProgressState:
    stage: str = STAGE_IDLE
    percent: float | None = None
    detail: str = ""
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressTracker:
    """Latest progress for every (session, processor) pair.

    In memory, with a persistence callback for the updates worth keeping. Losing
    it on restart is correct: progress describes work in flight, and after a
    restart there is none.
    """

    def __init__(self, on_change: Callable[[str, str, ProgressState], None] | None = None):
        self._states: dict[tuple[str, str], ProgressState] = {}
        self._lock = threading.Lock()
        self._on_change = on_change

    def report(
        self,
        session_key: str,
        processor: str,
        stage: str,
        percent: float | None = None,
        detail: str = "",
    ) -> bool:
        """Record progress. Returns whether it was significant enough to publish."""
        if percent is not None:
            percent = max(0.0, min(100.0, float(percent)))

        key = (session_key, processor)

        with self._lock:
            previous = self._states.get(key)
            state = ProgressState(stage=stage, percent=percent, detail=detail)
            self._states[key] = state

            significant = (
                previous is None
                or previous.stage != stage
                or previous.detail != detail
                or (percent is not None and previous.percent is None)
                or (
                    percent is not None
                    and previous.percent is not None
                    and abs(percent - previous.percent) >= PERCENT_EPSILON
                )
            )

        if significant and self._on_change is not None:
            try:
                self._on_change(session_key, processor, state)
            except Exception as error:
                # Reporting progress must never be able to break the work it is
                # reporting on.
                logger.warning("progress.publish_failed", error=str(error))

        return significant

    def for_session(self, session_key: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                processor: state.as_dict()
                for (key, processor), state in self._states.items()
                if key == session_key
            }

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            grouped: dict[str, dict[str, dict[str, Any]]] = {}

            for (session_key, processor), state in self._states.items():
                grouped.setdefault(session_key, {})[processor] = state.as_dict()

            return grouped

    def clear_session(self, session_key: str) -> None:
        with self._lock:
            for key in [key for key in self._states if key[0] == session_key]:
                del self._states[key]

    def bind(self, session_key: str, processor: str) -> Callable[..., None]:
        """A reporter bound to one processor in one session.

        This is what lands on `SessionContext.progress`, so a processor calls
        `session.report(stage, percent)` and never handles keys itself.
        """

        def report(stage: str, percent: float | None = None, detail: str = "") -> None:
            self.report(session_key, processor, stage, percent, detail)

        return report
