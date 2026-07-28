"""Live startrail stacking.

The stack is a running per-pixel maximum, updated the moment a frame lands, so the
finished startrail already exists at sunrise - there is never a pass over the
night's images. Its cost is one image decode and one C-level max per capture,
which is a fraction of a second even on a Pi, against a capture interval measured
in tens of seconds.

Not every frame of the session belongs in it. A night session opens at sunset and
closes at sunrise, so it begins and ends in twilight; with a fixed exposure those
frames come off the sensor almost white, and a per-pixel maximum has no way to
recover from one of them - a single white frame whites out the night. So the
stack runs between astronomical dusk and astronomical dawn, the window in which
the sun contributes no light at all to the sky, computed by astral for the
configured site. Frames outside it are counted and dropped.

The stack is also built from the original frame rather than the rendered one.
The rendered frame has the overlay burned in, and maximum-blending three hundred
different timestamps on top of each other turns the corner of the image into an
unreadable smear.

Two things are written every frame:

  * a downscaled preview, for the web UI to show the trails forming live
  * a build frame, which becomes one frame of the growth timelapse

and two things are written rarely:

  * the full-resolution working stack, checkpointed so a crash costs a few frames
    rather than the night
  * the final image, once at sunrise

The build frames are written at video resolution rather than full size on purpose.
A night of 4056x3040 PNGs is tens of gigabytes for an animation that will be
1920 wide; writing them at their eventual size costs nothing in quality and turns
that into a few hundred megabytes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import structlog

from app import astro
from app.processing.base import (
    CATEGORY_STARTRAIL,
    STAGE_COMPLETED,
    STAGE_FINALISING,
    STAGE_RUNNING,
    ConfigField,
    FrameEvent,
    ProductDraft,
    Processor,
    SessionContext,
    register_processor,
)
from app.processing.images import (
    lighten,
    load_if_exists,
    open_rgb,
    save_jpeg,
    save_png,
    scaled_to_width,
)

logger = structlog.get_logger()

STACK_FILENAME = "stack.png"

# How many frames went in and how many were dropped, kept beside the stack. The
# stack survives a restart, so the numbers describing it have to as well -
# otherwise a server rebooted at 2am reports a startrail built from the frames
# since the reboot.
COUNTS_FILENAME = "counts.json"


@register_processor
class StartrailProcessor(Processor):
    name = "startrail"
    label = "Startrail"
    description = (
        "Stacks every night frame with a lighten blend, live. The finished image "
        "exists at sunrise without reprocessing, and the growth is saved as a video."
    )

    # Daylight has no trails to keep, and a day stack is a uniformly blown-out sky.
    periods = frozenset({"night"})

    fields = (
        ConfigField(
            "sun_depression", "Stack below sun depression", "float", astro.ASTRONOMICAL,
            help_text="Degrees the sun must be below the horizon before a frame is "
                      "stacked. 18 is astronomical dusk and dawn - a sky the sun no "
                      "longer lights at all. Lower it to start the trail earlier at "
                      "the cost of a brighter background; 0 stacks the whole session, "
                      "sunset to sunrise.",
            minimum=0.0, maximum=90.0,
        ),
        ConfigField(
            "stack_source", "Stack from", "choice", "original",
            choices=["original", "rendered"],
            help_text="The original is the masked frame before the overlay is drawn "
                      "on it - stacking the rendered one blends every timestamp of "
                      "the night into a smear. The original has no hue correction, so "
                      "the trail's colour is the sensor's rather than the timelapse's.",
        ),
        ConfigField(
            "preview_width", "Live preview width", "int", 1280,
            help_text="Width of the preview the web UI polls while the night runs.",
            minimum=320, maximum=4096,
        ),
        ConfigField(
            "stack_width", "Stacking width", "int", 0,
            help_text="0 stacks at full sensor resolution. Lower it to cut memory and CPU.",
            minimum=0, maximum=8192,
        ),
        ConfigField(
            "jpeg_quality", "JPEG quality", "int", 92,
            minimum=40, maximum=100,
        ),
        ConfigField(
            "checkpoint_every", "Checkpoint every N frames", "int", 10,
            help_text="How often the full-resolution stack is written to disk. "
                      "A crash loses at most this many frames of trail.",
            minimum=1, maximum=500,
        ),
    )

    category = CATEGORY_STARTRAIL

    # ---- lifecycle ----

    def on_session_start(self, session: SessionContext) -> None:
        session.ensure_dirs()

        # Resume rather than restart: a server that was rebooted at 2am should
        # carry on stacking onto what it already had, not throw away four hours.
        recovered = load_if_exists(session.work_dir / STACK_FILENAME)

        session.state["stack"] = recovered
        session.state["since_checkpoint"] = 0
        session.state["counts"] = self._load_counts(session)
        session.state["window"] = self._dark_window(session)

        if recovered is not None:
            # Share immediately, so a dependent processor finalising a resumed
            # session sees the stack even if no new frame has arrived yet.
            session.share("stack", recovered)
            logger.info("startrail.resumed", session=session.session_key, size=recovered.size)

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        session.share("frame_index", session.frame_count)

        counts = session.state["counts"]
        excluded = self._exclusion(session, frame)

        if excluded:
            counts["skipped"] += 1

            # The build video watches this. Without it, it would append another
            # copy of the unchanged stack and open the animation with a long
            # motionless stretch covering the whole of twilight.
            session.share("frame_used", False)
            session.report(STAGE_RUNNING, detail=self._detail(counts, excluded))

            return ()

        incoming = open_rgb(self._source_path(session, frame))

        stack_width = int(session.config["stack_width"])

        if stack_width:
            incoming = scaled_to_width(incoming, stack_width)

        stack = session.state.get("stack")
        # stack = max(stack, newFrame). The first frame *is* the stack.
        stack = incoming if stack is None else lighten(stack, incoming)
        session.state["stack"] = stack
        counts["stacked"] += 1

        # Publish it for whoever consumes this. The build-video processor takes
        # the stack from here rather than recomputing it, which is the whole
        # reason it is a separate processor instead of code inside this one.
        session.share("stack", stack)
        session.share("frame_used", True)

        session.report(STAGE_RUNNING, detail=self._detail(counts))

        drafts: list[ProductDraft] = []

        preview = scaled_to_width(stack, int(session.config["preview_width"]))
        preview_path = session.output_dir / "startrail-live.jpg"
        save_jpeg(preview, preview_path, int(session.config["jpeg_quality"]))

        drafts.append(
            ProductDraft(
                kind="startrail_live",
                path=preview_path,
                media_type="image/jpeg",
                state="live",
                category=CATEGORY_STARTRAIL,
                frame_count=counts["stacked"],
                width=preview.width,
                height=preview.height,
            )
        )

        # Checkpointing the full stack is the expensive write, so it is the one
        # that does not happen every frame.
        session.state["since_checkpoint"] = session.state.get("since_checkpoint", 0) + 1

        if session.state["since_checkpoint"] >= int(session.config["checkpoint_every"]):
            save_png(stack, session.work_dir / STACK_FILENAME)
            self._save_counts(session)
            session.state["since_checkpoint"] = 0

        return drafts

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        stack = session.state.get("stack") or load_if_exists(session.work_dir / STACK_FILENAME)
        counts = session.state.get("counts") or self._load_counts(session)

        if stack is None:
            logger.info(
                "startrail.nothing_to_finalise",
                session=session.session_key,
                skipped=counts["skipped"],
            )
            # Say which of the two it was. "Nothing to stack" after a night of
            # captures is a puzzle; "every frame was above the threshold" is a
            # setting to go and change.
            session.report(
                STAGE_COMPLETED,
                100.0,
                f"No frame was dark enough to stack - {counts['skipped']} skipped"
                if counts["skipped"]
                else "Nothing to stack",
            )
            return ()

        session.report(STAGE_FINALISING, 50.0, "Writing the final stack")

        quality = int(session.config["jpeg_quality"])
        final_path = session.output_dir / "startrail.jpg"
        save_jpeg(stack, final_path, quality)

        # Published for the build-video processor, which finalises after this one
        # and needs the completed stack for its last frame.
        session.share("stack", stack)
        session.share("final", True)

        # The live preview is the same picture as the final one now. Refreshing it
        # means a UI still pointing at the live product shows the finished trail.
        save_jpeg(
            scaled_to_width(stack, int(session.config["preview_width"])),
            session.output_dir / "startrail-live.jpg",
            quality,
        )

        # Full-resolution PNG state is the largest thing the pipeline keeps and it
        # has no further use once the JPEG exists.
        (session.work_dir / STACK_FILENAME).unlink(missing_ok=True)
        (session.work_dir / COUNTS_FILENAME).unlink(missing_ok=True)

        session.report(STAGE_COMPLETED, 100.0, self._detail(counts))

        return [
            ProductDraft(
                kind="startrail",
                path=final_path,
                media_type="image/jpeg",
                state="final",
                category=CATEGORY_STARTRAIL,
                # What is in the picture, not what the night captured. The two
                # differ by however long twilight lasted.
                frame_count=counts["stacked"],
                width=stack.width,
                height=stack.height,
                metadata={"frames_skipped": counts["skipped"], **self._window_metadata(session)},
            )
        ]

    def on_shutdown(self, session: SessionContext) -> None:
        """Flush the stack so a restart resumes from now, not the last checkpoint."""
        stack = session.state.get("stack")

        if stack is not None:
            save_png(stack, session.work_dir / STACK_FILENAME)
            self._save_counts(session)
            logger.info("startrail.flushed", session=session.session_key)

    def on_resume(self, session: SessionContext) -> None:
        stack = session.state.get("stack")

        if stack is not None:
            session.report(
                STAGE_RUNNING, detail=f"Resumed on a {stack.width}×{stack.height} stack"
            )

    # ---- which frames belong in the stack ----

    def _dark_window(self, session: SessionContext) -> tuple[datetime, datetime] | None:
        """Astronomical dusk to dawn for this night, or None if it never gets there."""
        depression = float(session.config["sun_depression"])

        if not depression:
            return None

        try:
            night = date.fromisoformat(session.archive_date)

        except ValueError:
            logger.warning(
                "startrail.unreadable_night",
                session=session.session_key,
                archive_date=session.archive_date,
            )
            return None

        window = astro.dark_window(night, depression)

        if window is None:
            logger.info(
                "startrail.never_dark_enough",
                session=session.session_key,
                depression=depression,
            )
        else:
            logger.info(
                "startrail.dark_window",
                session=session.session_key,
                depression=depression,
                dusk=window[0].isoformat(),
                dawn=window[1].isoformat(),
            )

        return window

    def _exclusion(self, session: SessionContext, frame: FrameEvent) -> str | None:
        """Why this frame is not being stacked, or None if it is.

        A sentence rather than a flag, because it goes straight to the operator:
        a startrail that stays black for the first hour of the evening should say
        what it is waiting for, not look broken.
        """
        depression = float(session.config["sun_depression"])

        if not depression:
            return None

        moment = frame.captured_at

        # A frame that lost its timezone somewhere upstream is UTC by convention
        # everywhere else in the pipeline, and guessing here beats raising.
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        # The frame's own sun position decides, not the night's dusk and dawn
        # times. Same criterion, but it cannot attribute a frame to the wrong
        # night, and it stays right for a session that runs long or is opened by
        # hand at an odd hour.
        if astro.is_dark(moment, depression):
            return None

        window = session.state.get("window")

        if window is None:
            return (
                f"The sun never gets {depression:g}° below the horizon tonight - "
                "nothing dark enough to stack"
            )

        dusk, dawn = window

        return (
            f"Waiting for astronomical dusk at {dusk:%H:%M}"
            if moment < dusk
            else f"Astronomical dawn was {dawn:%H:%M}"
        )

    def _source_path(self, session: SessionContext, frame: FrameEvent) -> Path:
        """The frame to stack: the un-overlaid original unless asked otherwise."""
        if session.config["stack_source"] == "rendered":
            return frame.rendered_path

        original = frame.original_path

        # Originals can be turned off, and a frame from before this setting
        # existed may not have one. The rendered frame is always there.
        return original if original and original.is_file() else frame.rendered_path

    def _window_metadata(self, session: SessionContext) -> dict:
        window = session.state.get("window")

        if window is None:
            return {}

        return {"dark_from": window[0].isoformat(), "dark_until": window[1].isoformat()}

    # ---- progress and counts ----

    def _detail(self, counts: dict, waiting: str = "") -> str:
        if waiting and not counts["stacked"]:
            return waiting

        detail = f"{counts['stacked']} frames stacked"

        return f"{detail}, {counts['skipped']} skipped" if counts["skipped"] else detail

    def _load_counts(self, session: SessionContext) -> dict:
        path = session.work_dir / COUNTS_FILENAME

        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            return {"stacked": int(stored["stacked"]), "skipped": int(stored["skipped"])}

        except (OSError, ValueError, KeyError, TypeError):
            # No file yet on a fresh session, and a half-written one after a hard
            # kill. Neither is worth failing a night over.
            return {"stacked": 0, "skipped": 0}

    def _save_counts(self, session: SessionContext) -> None:
        counts = session.state.get("counts")

        if counts is None:
            return

        path = session.work_dir / COUNTS_FILENAME
        temporary = path.with_name(f".{path.name}.tmp")

        try:
            temporary.write_text(json.dumps(counts), encoding="utf-8")
            temporary.replace(path)

        except OSError as error:
            logger.warning("startrail.counts_unwritable", error=str(error))
