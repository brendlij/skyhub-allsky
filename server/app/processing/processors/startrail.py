"""Live startrail stacking.

The stack is a running per-pixel maximum, updated the moment a frame lands, so the
finished startrail already exists at sunrise - there is never a pass over the
night's images. Its cost is one image decode and one C-level max per capture,
which is a fraction of a second even on a Pi, against a capture interval measured
in tens of seconds.

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

from typing import Iterable

import structlog

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

        if recovered is not None:
            # Share immediately, so a dependent processor finalising a resumed
            # session sees the stack even if no new frame has arrived yet.
            session.share("stack", recovered)
            logger.info("startrail.resumed", session=session.session_key, size=recovered.size)

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        incoming = open_rgb(frame.rendered_path)

        stack_width = int(session.config["stack_width"])

        if stack_width:
            incoming = scaled_to_width(incoming, stack_width)

        stack = session.state.get("stack")
        # stack = max(stack, newFrame). The first frame *is* the stack.
        stack = incoming if stack is None else lighten(stack, incoming)
        session.state["stack"] = stack

        # Publish it for whoever consumes this. The build-video processor takes
        # the stack from here rather than recomputing it, which is the whole
        # reason it is a separate processor instead of code inside this one.
        session.share("stack", stack)
        session.share("frame_index", session.frame_count)

        session.report(STAGE_RUNNING, detail=f"{session.frame_count} frames stacked")

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
                frame_count=session.frame_count,
                width=preview.width,
                height=preview.height,
            )
        )

        # Checkpointing the full stack is the expensive write, so it is the one
        # that does not happen every frame.
        session.state["since_checkpoint"] = session.state.get("since_checkpoint", 0) + 1

        if session.state["since_checkpoint"] >= int(session.config["checkpoint_every"]):
            save_png(stack, session.work_dir / STACK_FILENAME)
            session.state["since_checkpoint"] = 0

        return drafts

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        stack = session.state.get("stack") or load_if_exists(session.work_dir / STACK_FILENAME)

        if stack is None:
            logger.info("startrail.nothing_to_finalise", session=session.session_key)
            session.report(STAGE_COMPLETED, 100.0, "Nothing to stack")
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

        session.report(STAGE_COMPLETED, 100.0, f"{session.frame_count} frames stacked")

        return [
            ProductDraft(
                kind="startrail",
                path=final_path,
                media_type="image/jpeg",
                state="final",
                category=CATEGORY_STARTRAIL,
                frame_count=session.frame_count,
                width=stack.width,
                height=stack.height,
            )
        ]

    def on_shutdown(self, session: SessionContext) -> None:
        """Flush the stack so a restart resumes from now, not the last checkpoint."""
        stack = session.state.get("stack")

        if stack is not None:
            save_png(stack, session.work_dir / STACK_FILENAME)
            logger.info("startrail.flushed", session=session.session_key)

    def on_resume(self, session: SessionContext) -> None:
        stack = session.state.get("stack")

        if stack is not None:
            session.report(
                STAGE_RUNNING, detail=f"Resumed on a {stack.width}×{stack.height} stack"
            )
