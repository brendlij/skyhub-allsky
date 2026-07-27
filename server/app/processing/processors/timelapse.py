"""Timelapse: a manifest during the night, one encode at the end.

The per-frame work is deliberately almost nothing - append a path to a text file.
Encoding is the heaviest thing the pipeline does and belongs where the spec puts
it, after the session ends, not competing with the camera for CPU while frames are
still arriving.

Keeping the manifest is what stops the encode from touching the archive. At
sunrise ffmpeg is handed a list the session has been writing all night, in capture
order, rather than being pointed at a directory to scan and sort.

The `variant` class attribute is the extension point: a weekly, monthly or
event-based timelapse is this class with a different frame source, and the
registry picks it up with no change to anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import structlog

from app.processing.base import (
    CATEGORY_TIMELAPSE,
    STAGE_COMPLETED,
    STAGE_ENCODING,
    STAGE_FAILED,
    ConfigField,
    FrameEvent,
    ProductDraft,
    Processor,
    SessionContext,
    register_processor,
)
from app.processing.video import container_for, encode_timelapse

logger = structlog.get_logger()

MANIFEST_FILENAME = "frames.txt"


@register_processor
class TimelapseProcessor(Processor):
    name = "timelapse"
    label = "Timelapse"
    description = (
        "Collects every capture of a session and encodes it into a video once the "
        "session closes. Night and day are separate sessions, so each gets its own."
    )

    session_kinds = frozenset({"day", "night"})
    category = CATEGORY_TIMELAPSE
    requires_ffmpeg = True

    fields = (
        ConfigField("fps", "Frames per second", "int", 30, minimum=1, maximum=120),
        ConfigField(
            "codec", "Codec", "choice", "h264",
            choices=["h264", "h265", "vp9"],
            help_text="h264 plays everywhere. h265 is smaller but slower to encode.",
        ),
        ConfigField(
            "width", "Width", "int", 1920,
            help_text="Height follows the source aspect ratio. 0 keeps the original size.",
            minimum=0, maximum=3840,
        ),
        ConfigField(
            "quality", "Quality (CRF)", "int", 23,
            help_text="Lower is better and larger. 18 is near-lossless, 28 is small.",
            minimum=0, maximum=51,
        ),
        ConfigField(
            "minimum_frames", "Minimum frames", "int", 10,
            help_text="Sessions shorter than this produce no video.",
            minimum=2, maximum=1000,
        ),
        ConfigField(
            "use_rendered", "Use rendered frames", "bool", True,
            help_text="Rendered frames carry the overlays and timestamp. Turn this "
                      "off to build the video from the untouched originals.",
        ),
    )

    # Which session this variant consumes. Subclasses override it to build
    # weekly or event-based videos from the same machinery.
    variant = "session"

    # ---- lifecycle ----

    def on_session_start(self, session: SessionContext) -> None:
        session.ensure_dirs()
        session.state["manifest"] = session.work_dir / MANIFEST_FILENAME

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        source = frame.rendered_path if session.config["use_rendered"] else (
            frame.original_path or frame.rendered_path
        )

        manifest: Path = session.state["manifest"]

        # Append rather than rewrite: this runs on every capture all night, and
        # the file is the only thing standing between a crash and a lost ordering.
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(f"{source}\n")

        return ()

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        frames = self._frames(session)
        minimum = int(session.config["minimum_frames"])

        if len(frames) < minimum:
            logger.info(
                "timelapse.too_short",
                session=session.session_key,
                frames=len(frames),
                minimum=minimum,
            )
            session.report(
                STAGE_COMPLETED, 100.0, f"Only {len(frames)} frames; {minimum} needed"
            )
            return ()

        codec = str(session.config["codec"])
        output_path = session.output_dir / f"timelapse-{session.period}.{container_for(codec)}"

        session.report(STAGE_ENCODING, 0.0, f"Encoding {len(frames)} frames")

        result = encode_timelapse(
            frames,
            output_path,
            fps=int(session.config["fps"]),
            codec=codec,
            quality=int(session.config["quality"]),
            width=int(session.config["width"]),
            manifest_path=session.work_dir / "encode.concat",
            on_progress=lambda percent: session.report(STAGE_ENCODING, percent),
        )

        if not result.ok:
            session.report(STAGE_FAILED, detail=result.error or "Encoding failed")

            return [
                ProductDraft(
                    kind="timelapse",
                    path=output_path,
                    media_type=f"video/{container_for(codec)}",
                    state="failed",
                    category=CATEGORY_TIMELAPSE,
                    frame_count=len(frames),
                    metadata={"error": result.error},
                )
            ]

        # The manifest has done its job. Left in place it would grow a few hundred
        # kilobytes per night per node forever, and a session that reopens for a
        # late frame rebuilds it from that frame on.
        (session.work_dir / MANIFEST_FILENAME).unlink(missing_ok=True)

        session.report(STAGE_COMPLETED, 100.0, f"{result.frame_count} frames encoded")

        return [
            ProductDraft(
                kind="timelapse",
                path=output_path,
                media_type=f"video/{container_for(codec)}",
                state="final",
                category=CATEGORY_TIMELAPSE,
                frame_count=result.frame_count,
                duration_seconds=result.duration_seconds,
                metadata={"fps": int(session.config["fps"]), "codec": codec},
            )
        ]

    # ---- frame source ----

    def _frames(self, session: SessionContext) -> list[Path]:
        """Read back the manifest, in capture order, skipping what has gone.

        The manifest is the record of what was captured; the filesystem is the
        record of what still exists. Retention can have removed frames from under
        a running night, so the two are reconciled here rather than assumed equal.
        """
        manifest: Path = session.state.get("manifest") or (session.work_dir / MANIFEST_FILENAME)

        if not manifest.is_file():
            return []

        frames: list[Path] = []
        seen: set[str] = set()

        for line in manifest.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()

            # A restart can replay a frame into the manifest; a duplicate would
            # show up as a stutter in the video.
            if not candidate or candidate in seen:
                continue

            seen.add(candidate)
            path = Path(candidate)

            if path.is_file():
                frames.append(path)

        return frames
