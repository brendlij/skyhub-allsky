"""The startrail growth video - a processor that consumes another processor.

This is the worked example of the dependency model. It never opens a capture and
never stacks anything: it takes the stack the startrail processor has already
computed for this frame, writes it out as one frame of an animation, and encodes
them when the session ends.

    Capture → Startrail → Startrail build video

The coupling is deliberately loose. The dependency is read through
`session.consume("startrail", "stack")`, which returns None when the startrail
processor is disabled, has failed, or has not produced anything yet. This
processor then does nothing that frame rather than raising - so turning the
startrail off degrades this to silence instead of an error every twenty seconds.

Splitting it out of the startrail processor buys three things: the growth video
can be turned off without touching stacking, a failure in ffmpeg cannot take the
startrail image down with it, and the pattern is now demonstrated for the
processors that come later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image
import structlog

from app.processing.base import (
    CATEGORY_STARTRAIL,
    STAGE_COMPLETED,
    STAGE_ENCODING,
    STAGE_FAILED,
    STAGE_RUNNING,
    ConfigField,
    FrameEvent,
    ProductDraft,
    Processor,
    SessionContext,
    register_processor,
)
from app.processing.images import even_dimensions, save_jpeg, scaled_to_width
from app.processing.video import container_for, encode_timelapse

logger = structlog.get_logger()

BUILD_DIRNAME = "build"


@register_processor
class StartrailBuildProcessor(Processor):
    name = "startrail_build"
    label = "Startrail build video"
    description = (
        "Renders the trails growing over the night, one frame per capture, using "
        "the stack the startrail processor already computed."
    )

    session_kinds = frozenset({"night"})
    category = CATEGORY_STARTRAIL
    requires_ffmpeg = True

    # Ordering only. The value is read through `consume`, which tolerates it
    # being absent.
    depends_on = ("startrail",)

    # After the startrail by default, so its frame is the stack including the
    # capture that has just landed rather than the one before it.
    default_priority = 110

    fields = (
        ConfigField("fps", "Frames per second", "int", 30, minimum=1, maximum=120),
        ConfigField(
            "width", "Width", "int", 1920,
            help_text="Build frames are written at this size, not at sensor "
                      "resolution — a night of full-size stills would be tens of gigabytes.",
            minimum=320, maximum=3840,
        ),
        ConfigField("codec", "Codec", "choice", "h264", choices=["h264", "h265", "vp9"]),
        ConfigField("quality", "Quality (CRF)", "int", 23, minimum=0, maximum=51),
        ConfigField(
            "frame_quality", "Build frame JPEG quality", "int", 88,
            help_text="These are intermediates a codec will recompress anyway.",
            minimum=40, maximum=100,
        ),
    )

    # ---- lifecycle ----

    def on_session_start(self, session: SessionContext) -> None:
        session.ensure_dirs()

        build_dir = session.work_dir / BUILD_DIRNAME
        build_dir.mkdir(parents=True, exist_ok=True)

        session.state["build_dir"] = build_dir
        session.state["frames"] = sorted(build_dir.glob("frame_*.jpg"))

        if session.state["frames"]:
            logger.info(
                "startrail_build.resumed",
                session=session.session_key,
                frames=len(session.state["frames"]),
            )

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        stack: Image.Image | None = session.consume("startrail", "stack")

        if stack is None:
            # The startrail processor is off, failed, or has not run yet. Nothing
            # to animate, and nothing to complain about.
            return ()

        # The startrail rejects frames from either end of the night, when the sun
        # is still up far enough to wash the sky out. Those frames leave the stack
        # untouched, so animating them would open the video with a still image
        # held for the length of twilight. Default True: a startrail from before
        # this existed publishes no such key and every frame counted.
        if not session.consume("startrail", "frame_used", True):
            return ()

        frames: list[Path] = session.state["frames"]

        # Even dimensions here rather than in ffmpeg's filter: every frame must
        # agree, and the scale filter would round each one independently.
        image = even_dimensions(scaled_to_width(stack, int(session.config["width"])))
        path = session.state["build_dir"] / f"frame_{len(frames):06d}.jpg"

        save_jpeg(image, path, int(session.config["frame_quality"]))
        frames.append(path)

        session.report(STAGE_RUNNING, detail=f"{len(frames)} build frames")

        return ()

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        frames: list[Path] = session.state.get("frames") or []

        if len(frames) < 2:
            session.report(STAGE_COMPLETED, 100.0, "Too few frames for a video")
            return ()

        codec = str(session.config["codec"])
        output_path = session.output_dir / f"startrail-build.{container_for(codec)}"

        session.report(STAGE_ENCODING, 0.0, f"Encoding {len(frames)} frames")

        result = encode_timelapse(
            frames,
            output_path,
            fps=int(session.config["fps"]),
            codec=codec,
            quality=int(session.config["quality"]),
            width=int(session.config["width"]),
            manifest_path=session.work_dir / "build.concat",
            on_progress=lambda percent: session.report(STAGE_ENCODING, percent),
        )

        if not result.ok:
            session.report(STAGE_FAILED, detail=result.error or "Encoding failed")

            return [
                ProductDraft(
                    kind="startrail_build",
                    path=output_path,
                    media_type=f"video/{container_for(codec)}",
                    state="failed",
                    category=CATEGORY_STARTRAIL,
                    frame_count=len(frames),
                    metadata={"error": result.error},
                )
            ]

        # Only once the video exists: these are hundreds of megabytes, and until
        # the encode succeeded they are the only copy of the animation.
        for frame_path in frames:
            frame_path.unlink(missing_ok=True)

        session.report(STAGE_COMPLETED, 100.0, f"{result.frame_count} frames encoded")

        return [
            ProductDraft(
                kind="startrail_build",
                path=output_path,
                media_type=f"video/{container_for(codec)}",
                state="final",
                category=CATEGORY_STARTRAIL,
                frame_count=result.frame_count,
                duration_seconds=result.duration_seconds,
                metadata={"fps": int(session.config["fps"]), "codec": codec},
            )
        ]
