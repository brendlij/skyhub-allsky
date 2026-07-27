"""The pipeline: a bounded queue, one worker, and strict isolation.

The capture endpoint publishes a frame and returns. Everything after that happens
here, off the request path, so a slow processor delays no upload and a broken one
takes nothing else down with it.

Three rules the whole design hangs on:

**The queue is bounded and drops the oldest.** A processor that cannot keep up
with the capture interval must cost frames, not memory. On a Pi an unbounded
backlog of decoded images is an out-of-memory kill that stops the camera, which is
strictly worse than a keogram missing a column.

**One worker, not a pool.** The processors are CPU-bound image work on a machine
that is also running a camera. Two workers would contend for the same cores and
finish no sooner, and serialising means a session's state needs no locking.

**A processor's failure is its own.** Every hook is called inside a guard; an
exception marks that processor failed for that session, is recorded on the session
row, and the remaining processors run as though nothing happened.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import structlog

from app.config import get_settings
from app.db.database import SessionLocal
from app.processing.base import (
    STAGE_FAILED,
    STAGE_FINALISING,
    FrameEvent,
    ProductDraft,
    Processor,
    SessionContext,
    registered_processors,
    resolve_order,
    session_key_for,
)
from app.processing.products import manager as product_manager
from app.processing.progress import ProgressState, ProgressTracker
from app.processing.video import ffmpeg_available
from app.repositories.processing_repository import (
    ProcessingSessionRepository,
    ProcessingSettingsRepository,
)

logger = structlog.get_logger()

# Published so the UI can refresh without polling. Injected rather than imported
# to keep this module free of any dependency on the web layer.
Broadcaster = Callable[[dict[str, Any]], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Session:
    """A live session: its database key plus one context per active processor."""

    def __init__(self, node_id: str, archive_date: str, period: str, session_kind: str = "solar"):
        self.node_id = node_id
        self.archive_date = archive_date
        self.period = period
        # How the session is driven: "solar" opens and closes with the sun,
        # anything else only by explicit request.
        self.session_kind = session_kind
        self.key = session_key_for(node_id, archive_date, period)
        self.contexts: dict[str, SessionContext] = {}
        # The order processors run in, dependencies first. Computed once when the
        # session opens rather than per frame.
        self.order: list[str] = []
        # The one place processors can see each other. Owned by the session so
        # every context shares the same dict and a value published by one is
        # visible to the next in the same frame.
        self.shared: dict[str, dict[str, Any]] = {}
        # A processor that raised is dropped for the rest of the session rather
        # than retried on every frame - the failure is nearly always systemic
        # (no disk, no permission) and retrying just fills the log.
        self.failed: set[str] = set()
        # Conditions the session's frames were taken under, carried onto every
        # product so a startrail records the exposure and temperature that made
        # it without each processor having to collect them.
        self.ambient: dict[str, Any] = {}


class ProcessingPipeline:
    def __init__(self):
        self._queue: asyncio.Queue[FrameEvent | None] | None = None
        self._worker: asyncio.Task | None = None
        self._sessions: dict[str, _Session] = {}
        self._broadcast: Broadcaster | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._progress = ProgressTracker(on_change=self._on_progress)
        self._dropped = 0
        self._processed = 0

    def _on_progress(self, session_key: str, processor: str, state: ProgressState) -> None:
        """Persist and publish a progress change.

        Called from the worker thread, so the broadcast is handed back to the
        event loop rather than awaited here. Only significant changes reach this -
        the tracker filters out the noise of a percentage creeping upward.
        """
        with SessionLocal() as db:
            ProcessingSessionRepository(db).set_progress(session_key, processor, state.as_dict())

        if self._broadcast is None or self._loop is None:
            return

        node_id, _, remainder = session_key.partition("/")
        archive_date, _, period = remainder.partition("/")

        asyncio.run_coroutine_threadsafe(
            self._broadcast(
                {
                    "type": "processing.progress",
                    "node_id": node_id,
                    "archive_date": archive_date,
                    "period": period,
                    "processor": processor,
                    **state.as_dict(),
                }
            ),
            self._loop,
        )

    # ---- lifecycle ----

    async def start(self, broadcast: Broadcaster | None = None) -> None:
        settings = get_settings()

        if not settings.processing_enabled:
            logger.info("processing.disabled")
            return

        self._broadcast = broadcast
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=max(8, settings.processing_queue_size))
        self._worker = asyncio.create_task(self._run())

        logger.info(
            "processing.started",
            processors=sorted(registered_processors()),
            ffmpeg=ffmpeg_available(),
            queue_size=self._queue.maxsize,
        )

        await asyncio.to_thread(self._recover_open_sessions)

    async def stop(self) -> None:
        """Drain what is queued, then close cleanly.

        Sessions are deliberately *not* finalised on shutdown: a restart should
        resume the night, not encode half of it. The working state on disk is what
        makes that safe.
        """
        if self._queue is not None:
            await self._queue.put(None)

        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()

        # Let every processor flush in-memory state to disk, so a restart resumes
        # from where the process stopped rather than from the last checkpoint.
        await asyncio.to_thread(self._shutdown_sessions)

        logger.info("processing.stopped", processed=self._processed, dropped=self._dropped)

    def _shutdown_sessions(self) -> None:
        for session in self._sessions.values():
            for name, context in session.contexts.items():
                if name in session.failed:
                    continue

                try:
                    context.state["_instance"].on_shutdown(context)

                except Exception as error:
                    # Shutting down is the one time a failure has nowhere useful
                    # to go, so it is logged and stepped over.
                    logger.warning(
                        "processing.shutdown_hook_failed",
                        session=session.key,
                        processor=name,
                        error=str(error),
                    )

    # ---- publishing ----

    def publish(self, frame: FrameEvent) -> bool:
        """Hand a capture to the pipeline. Never blocks, never raises.

        Called from the upload endpoint, which must return to the camera node
        promptly whatever the pipeline is doing.
        """
        if self._queue is None:
            return False

        try:
            self._queue.put_nowait(frame)
            return True

        except asyncio.QueueFull:
            # Drop the oldest: a keogram missing an older column is better than
            # one missing the column for right now, which is what the UI shows.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

            self._dropped += 1
            logger.warning("processing.frame_dropped", session=frame.session_key, dropped=self._dropped)

            return False

    # ---- worker ----

    async def _run(self) -> None:
        assert self._queue is not None

        while True:
            frame = await self._queue.get()

            try:
                if frame is None:
                    return

                # to_thread keeps the event loop free: everything below is Pillow
                # and disk, and both release the GIL.
                drafts = await asyncio.to_thread(self._handle_frame, frame)
                self._processed += 1

                if drafts:
                    await self._announce(frame.node_id, frame.archive_date, frame.period, drafts)

            except Exception as error:
                # The worker must outlive anything a processor can do to it.
                logger.warning("processing.frame_failed", error=str(error), exc_info=True)

            finally:
                self._queue.task_done()

    # ---- frame handling (worker thread) ----

    def _handle_frame(self, frame: FrameEvent) -> list[tuple[str, ProductDraft]]:
        session = self._sessions.get(frame.session_key)

        if session is None:
            session = self._open_session(frame.node_id, frame.archive_date, frame.period)

        produced: list[tuple[str, ProductDraft]] = []

        with SessionLocal() as db:
            ProcessingSessionRepository(db).record_frame(session.key, frame.captured_at)

        self._collect_ambient(session, frame)

        for name, processor, context in self._active(session, frame.period):
            context.frame_count += 1

            try:
                drafts = processor.on_frame(context, frame) or ()

            except Exception as error:
                self._mark_failed(session, name, error, "on_frame")
                continue

            for draft in drafts:
                self._record_product(session, name, draft)
                produced.append((name, draft))

        return produced

    # Metadata worth carrying onto a product, and where to find it in a frame's
    # capture metadata. Anything absent is simply left out - a node without a
    # weather sensor produces products without weather fields, not products with
    # nulls in them.
    AMBIENT_FIELDS = {
        "exposure_ms": ("ExposureTime", "exposure_ms", "exposure_time"),
        "gain": ("AnalogueGain", "gain", "analogue_gain"),
        "temperature_c": ("temperature_c", "SensorTemperature"),
        "humidity_percent": ("humidity_percent",),
        "pressure_hpa": ("pressure_hpa",),
        "sqm": ("sqm", "sky_quality"),
        "moon_altitude": ("moon_altitude",),
        "sun_altitude": ("sun_altitude",),
    }

    def _collect_ambient(self, session: _Session, frame: FrameEvent) -> None:
        """Track the conditions across a session, for stamping onto its products.

        Ranges rather than a single value: a startrail is made of hundreds of
        frames, so "the exposure" is not one number. First and last frame times
        bound the session, and the numeric fields keep their min and max.
        """
        ambient = session.ambient
        metadata = frame.metadata or {}

        ambient.setdefault("first_frame_at", frame.captured_at.isoformat())
        ambient["last_frame_at"] = frame.captured_at.isoformat()
        ambient["node_id"] = frame.node_id

        if frame.width and frame.height:
            ambient["source_resolution"] = f"{frame.width}x{frame.height}"

        for name, keys in self.AMBIENT_FIELDS.items():
            value = next(
                (metadata[key] for key in keys if isinstance(metadata.get(key), (int, float))),
                None,
            )

            if value is None:
                continue

            low, high = ambient.get(f"{name}_range", (value, value))
            ambient[f"{name}_range"] = (min(low, value), max(high, value))

    def _active(self, session: _Session, period: str) -> Iterable[tuple[str, Processor, SessionContext]]:
        """Processors that are enabled, apply to this session, and have not failed.

        Yielded in dependency order, so a processor that consumes another's output
        always sees the value published for the frame being handled rather than
        the previous one.
        """
        for name in session.order:
            if name in session.failed:
                continue

            context = session.contexts.get(name)
            processor_class = registered_processors().get(name)

            if context is None or processor_class is None or not processor_class.runs_for(period):
                continue

            yield name, context.state["_instance"], context

    # ---- sessions ----

    def _open_session(
        self,
        node_id: str,
        archive_date: str,
        period: str,
        session_kind: str = "solar",
        label: str | None = None,
    ) -> _Session:
        settings = get_settings()
        session = _Session(node_id, archive_date, period, session_kind)

        with SessionLocal() as db:
            repository = ProcessingSessionRepository(db)
            record = repository.open(
                session.key, node_id, archive_date, period, session_kind=session_kind, label=label
            )
            # A session that already has frames is one this process did not start:
            # the server restarted mid-night, or a late frame reopened a closed
            # session. Either way the processors reload state rather than begin.
            resuming = (record.frame_count or 0) > 0

            settings_repo = ProcessingSettingsRepository(db)
            candidates: dict[str, int] = {}
            configs: dict[str, dict[str, Any]] = {}

            for name, processor_class in registered_processors().items():
                if not processor_class.runs_for(period):
                    continue

                stored = settings_repo.get_or_create(processor_class)

                if not stored.enabled:
                    continue

                if processor_class.requires_ffmpeg and not ffmpeg_available():
                    # Registered but inert: the product list will say why rather
                    # than the night simply producing nothing.
                    logger.warning("processing.ffmpeg_missing", processor=name)

                candidates[name] = stored.priority
                configs[name] = processor_class.coerce_config(stored.config)

        # Dependencies first, priority as the tie-break among independents.
        session.order = resolve_order(candidates, candidates)

        for name in session.order:
            processor_class = registered_processors()[name]

            context = SessionContext(
                node_id=node_id,
                archive_date=archive_date,
                period=period,
                config=configs[name],
                work_dir=settings.processing_state_dir / node_id / archive_date / period / name,
                output_dir=settings.derived_dir / node_id / archive_date / period,
                started_at=utc_now(),
                processor_name=name,
                shared=session.shared,
                progress=self._progress.bind(session.key, name),
            )
            context.state["_instance"] = processor_class()
            context.state["_priority"] = candidates[name]

            try:
                context.state["_instance"].on_session_start(context)

                if resuming:
                    context.state["_instance"].on_resume(context)

            except Exception as error:
                self._mark_failed(session, name, error, "on_resume" if resuming else "on_session_start")
                continue

            session.contexts[name] = context

        self._sessions[session.key] = session

        logger.info(
            "processing.session_opened",
            session=session.key,
            kind=session_kind,
            resuming=resuming,
            order=session.order,
        )

        return session

    async def open_manual_session(
        self, node_id: str, archive_date: str, period: str, label: str | None = None
    ) -> dict[str, Any]:
        """Open a session the sun did not open.

        Everything else about it is identical - the same processors, the same
        products, the same finalisation. The only difference is `session_kind`,
        which keeps the period watcher from closing it at the next sunrise: a
        session someone started by hand is theirs to end.
        """
        key = session_key_for(node_id, archive_date, period)

        if key in self._sessions:
            return {"session": key, "status": "already_open"}

        session = await asyncio.to_thread(
            self._open_session, node_id, archive_date, period, "manual", label
        )

        await self._announce_status(node_id, archive_date, period, "open")

        return {
            "session": key,
            "status": "open",
            "processors": list(session.order),
            "label": label,
        }

    async def close_session(self, node_id: str, archive_date: str, period: str) -> dict[str, Any]:
        """Finalise a session: run every processor's expensive step, then release it.

        Awaited from the sunrise/sunset watcher and from the API. The work happens
        in a thread so a fifteen-minute encode does not stall the event loop, the
        WebSocket feed, or the uploads still arriving for the *other* period.
        """
        key = session_key_for(node_id, archive_date, period)
        session = self._sessions.get(key)

        if session is None:
            session = await asyncio.to_thread(self._rehydrate_session, node_id, archive_date, period)

        if session is None:
            return {"session": key, "status": "not_found", "products": []}

        with SessionLocal() as db:
            ProcessingSessionRepository(db).set_status(key, "closing")

        await self._announce_status(node_id, archive_date, period, "closing")

        drafts = await asyncio.to_thread(self._finalise, session)

        # Nothing is live once the session has ended. Done after finalisation so
        # the final products exist to supersede the live ones.
        await asyncio.to_thread(product_manager.settle_session, key)

        self._sessions.pop(key, None)

        with SessionLocal() as db:
            ProcessingSessionRepository(db).set_status(
                key, "failed" if len(session.failed) == len(session.contexts) and session.contexts else "closed"
            )

        # Progress describes work in flight; there is none once a session closes.
        self._progress.clear_session(key)

        if drafts:
            await self._announce(node_id, archive_date, period, drafts)

        await self._announce_status(node_id, archive_date, period, "closed")

        logger.info("processing.session_closed", session=key, products=len(drafts))

        return {
            "session": key,
            "status": "closed",
            "products": [draft.kind for _, draft in drafts],
            "failed_processors": sorted(session.failed),
        }

    def _finalise(self, session: _Session) -> list[tuple[str, ProductDraft]]:
        produced: list[tuple[str, ProductDraft]] = []

        # Dependency order again: a processor that consumes another's final output
        # - the build video wants the completed stack - has to run after it.
        for name in session.order:
            context = session.contexts.get(name)

            if context is None or name in session.failed:
                continue

            processor: Processor = context.state["_instance"]
            self._progress.report(session.key, name, STAGE_FINALISING)

            try:
                drafts = processor.on_session_end(context) or ()

            except Exception as error:
                self._mark_failed(session, name, error, "on_session_end")
                continue

            for draft in drafts:
                self._record_product(session, name, draft)
                produced.append((name, draft))

            # The context holds the decoded stack; a closed session must not keep
            # tens of megabytes alive until the process restarts.
            context.state.pop("stack", None)
            context.state.pop("strip", None)

        return produced

    def _rehydrate_session(self, node_id: str, archive_date: str, period: str) -> _Session | None:
        """Rebuild a session the process did not open - after a restart.

        The processors reload their own state from `work_dir` in on_session_start,
        so finalising a session this server never saw a frame of works exactly the
        same as finalising a live one.
        """
        key = session_key_for(node_id, archive_date, period)

        with SessionLocal() as db:
            record = ProcessingSessionRepository(db).get(key)

            if record is None:
                return None

        session = self._open_session(node_id, archive_date, period)

        with SessionLocal() as db:
            stored = ProcessingSessionRepository(db).get(key)

            if stored is not None:
                for context in session.contexts.values():
                    context.frame_count = stored.frame_count

        return session

    def _recover_open_sessions(self) -> None:
        """Report sessions left open by a restart. They resume on the next frame."""
        with SessionLocal() as db:
            open_sessions = ProcessingSessionRepository(db).list_open()

        for record in open_sessions:
            logger.info(
                "processing.session_pending",
                session=record.session_key,
                frames=record.frame_count,
                detail="Resumes on the next capture, or finalises at the next period change.",
            )

    # ---- products and errors ----

    def _record_product(self, session: _Session, processor: str, draft: ProductDraft) -> None:
        """Hand the draft to the Derived Product Manager.

        Everything a product needs to be true - variants, versioning, metadata,
        the database row - is the manager's job. The pipeline's only contribution
        is the session context the processor did not have to know about, and the
        ambient conditions the product was made under.
        """
        if not draft.category:
            # Fall back to the processor's declared category, so a draft only has
            # to say when it differs from the rest of that processor's output.
            processor_class = registered_processors().get(processor)
            draft.category = processor_class.category if processor_class else ""

        product_manager.register(
            draft,
            node_id=session.node_id,
            archive_date=session.archive_date,
            period=session.period,
            processor=processor,
            session_key=session.key,
            ambient_metadata=session.ambient,
        )

    def _mark_failed(self, session: _Session, processor: str, error: Exception, hook: str) -> None:
        session.failed.add(processor)

        logger.warning(
            "processing.processor_failed",
            session=session.key,
            processor=processor,
            hook=hook,
            error=str(error),
            exc_info=True,
        )

        self._progress.report(session.key, processor, STAGE_FAILED, detail=str(error)[:200])

        with SessionLocal() as db:
            ProcessingSessionRepository(db).set_processor_state(
                session.key, processor, {"failed_at": utc_now().isoformat(), "hook": hook, "error": str(error)[:500]}
            )

        # Let the processor release whatever the failure stranded. It cannot
        # rescue itself - it is already disabled for this session - and anything
        # it raises here is swallowed, because a failure handler that fails must
        # not become a second failure.
        context = session.contexts.get(processor)

        if context is None:
            return

        try:
            context.state["_instance"].on_error(context, hook, error)

        except Exception as nested:
            logger.warning(
                "processing.error_hook_failed", processor=processor, error=str(nested)
            )

    # ---- dashboard ----

    async def _announce(
        self, node_id: str, archive_date: str, period: str, drafts: list[tuple[str, ProductDraft]]
    ) -> None:
        if self._broadcast is None:
            return

        await self._broadcast(
            {
                "type": "processing.products",
                "node_id": node_id,
                "archive_date": archive_date,
                "period": period,
                "products": [
                    {
                        "processor": processor,
                        "kind": draft.kind,
                        "state": draft.state,
                        "frame_count": draft.frame_count,
                    }
                    for processor, draft in drafts
                ],
            }
        )

    async def _announce_status(self, node_id: str, archive_date: str, period: str, status: str) -> None:
        if self._broadcast is None:
            return

        await self._broadcast(
            {
                "type": "processing.session",
                "node_id": node_id,
                "archive_date": archive_date,
                "period": period,
                "status": status,
            }
        )

    # ---- introspection ----

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._worker is not None and not self._worker.done(),
            "queued": self._queue.qsize() if self._queue else 0,
            "queue_size": self._queue.maxsize if self._queue else 0,
            "processed": self._processed,
            "dropped": self._dropped,
            "open_sessions": sorted(self._sessions),
            "ffmpeg": ffmpeg_available(),
        }

    def progress_for(self, session_key: str) -> dict[str, Any]:
        """Live progress for a session, empty when it is not running here."""
        return self._progress.for_session(session_key)

    def progress_snapshot(self) -> dict[str, Any]:
        return self._progress.snapshot()


pipeline = ProcessingPipeline()
