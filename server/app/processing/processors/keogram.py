"""Keogram: one column per capture, appended as the night runs.

A keogram compresses a whole night into one image. Each capture contributes a
single strip - by default the vertical centre line of the frame, which for a
circular allsky lens is horizon to horizon through the zenith - and the strips sit
side by side in time order. Cloud arrives as a vertical smear, moonrise as a
brightening ramp, a passing front as a hard edge.

The canvas grows by doubling rather than being rebuilt. Pasting into a
preallocated image is O(1) per frame; recreating a strip that is one column wider
each time would be O(n) per frame and O(n²) over a night, which on a 12-hour night
at 20-second intervals is the difference between imperceptible and minutes.
"""

from __future__ import annotations

from typing import Iterable

from PIL import Image
import structlog

from app.processing.base import (
    CATEGORY_KEOGRAM,
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
from app.processing.images import load_if_exists, open_rgb, save_jpeg, save_png

logger = structlog.get_logger()

STRIP_FILENAME = "keogram.png"

# Start at a night's worth of frames so the common case never reallocates.
INITIAL_CAPACITY = 1024


@register_processor
class KeogramProcessor(Processor):
    name = "keogram"
    label = "Keogram"
    description = (
        "Builds a night-at-a-glance strip, one column per capture, updated live. "
        "Cloud, moonrise and twilight all read straight off it."
    )

    session_kinds = frozenset({"day", "night"})
    category = CATEGORY_KEOGRAM

    fields = (
        ConfigField(
            "orientation", "Extraction line", "choice", "vertical",
            choices=["vertical", "horizontal"],
            help_text="Vertical takes a column through the frame; horizontal takes a row.",
        ),
        ConfigField(
            "position", "Line position", "float", 0.5,
            help_text="Fraction across the frame. 0.5 is the centre.",
            minimum=0.0, maximum=1.0,
        ),
        ConfigField(
            "height", "Keogram height", "int", 1080,
            help_text="Each strip is resized to this before being appended.",
            minimum=120, maximum=4096,
        ),
        ConfigField(
            "column_width", "Column width", "int", 2,
            help_text="Pixels per capture. Raise it for short nights so the result "
                      "is not a sliver.",
            minimum=1, maximum=32,
        ),
        ConfigField("jpeg_quality", "JPEG quality", "int", 90, minimum=40, maximum=100),
    )

    # ---- lifecycle ----

    def on_session_start(self, session: SessionContext) -> None:
        session.ensure_dirs()

        recovered = load_if_exists(session.work_dir / STRIP_FILENAME)

        # The recovered strip is exactly as wide as the columns already in it, so
        # its width is the resume point. Capacity starts there and doubles from on.
        session.state["strip"] = recovered
        session.state["columns"] = 0 if recovered is None else recovered.width // max(
            1, int(session.config["column_width"])
        )
        session.state["capacity"] = 0 if recovered is None else recovered.width

        if recovered is not None:
            logger.info(
                "keogram.resumed", session=session.session_key, columns=session.state["columns"]
            )

    def on_frame(self, session: SessionContext, frame: FrameEvent) -> Iterable[ProductDraft]:
        from app.processing.images import extract_strip

        height = int(session.config["height"])
        column_width = int(session.config["column_width"])

        image = open_rgb(frame.rendered_path)
        strip = extract_strip(image, str(session.config["orientation"]), float(session.config["position"]))

        # A horizontal extraction comes out as a 1px-tall row; rotate it so both
        # orientations append the same way and the keogram always reads left to
        # right in time.
        if session.config["orientation"] == "horizontal":
            strip = strip.transpose(Image.ROTATE_90)

        column = strip.resize((column_width, height), Image.LANCZOS)

        canvas = self._ensure_capacity(session, column_width, height)
        canvas.paste(column, (session.state["columns"] * column_width, 0))
        session.state["columns"] += 1

        used_width = session.state["columns"] * column_width
        visible = canvas.crop((0, 0, used_width, height))

        live_path = session.output_dir / "keogram-live.jpg"
        save_jpeg(visible, live_path, int(session.config["jpeg_quality"]))

        # Working state is written every frame here, unlike the startrail: a
        # keogram strip is a fraction of the size of a full-resolution stack, so
        # there is nothing to gain from risking columns to save the write.
        save_png(visible, session.work_dir / STRIP_FILENAME)

        session.report(STAGE_RUNNING, detail=f"{session.state['columns']} columns")

        return [
            ProductDraft(
                kind="keogram_live",
                path=live_path,
                media_type="image/jpeg",
                state="live",
                category=CATEGORY_KEOGRAM,
                frame_count=session.state["columns"],
                width=visible.width,
                height=visible.height,
            )
        ]

    def on_session_end(self, session: SessionContext) -> Iterable[ProductDraft]:
        canvas = session.state.get("strip")
        columns = session.state.get("columns", 0)

        session.report(STAGE_FINALISING, 50.0, "Writing the final keogram")

        if canvas is None or columns == 0:
            recovered = load_if_exists(session.work_dir / STRIP_FILENAME)

            if recovered is None:
                session.report(STAGE_COMPLETED, 100.0, "No columns collected")
                return ()

            canvas, columns = recovered, recovered.width // max(1, int(session.config["column_width"]))

        width = min(canvas.width, columns * int(session.config["column_width"]))
        final = canvas.crop((0, 0, width, canvas.height))

        final_path = session.output_dir / "keogram.jpg"
        save_jpeg(final, final_path, int(session.config["jpeg_quality"]))

        (session.work_dir / STRIP_FILENAME).unlink(missing_ok=True)

        session.report(STAGE_COMPLETED, 100.0, f"{columns} columns")

        return [
            ProductDraft(
                kind="keogram",
                path=final_path,
                media_type="image/jpeg",
                state="final",
                category=CATEGORY_KEOGRAM,
                frame_count=columns,
                width=final.width,
                height=final.height,
            )
        ]

    def on_shutdown(self, session: SessionContext) -> None:
        """The strip is already written every frame, so this only logs the handover."""
        logger.info(
            "keogram.flushed", session=session.session_key, columns=session.state.get("columns", 0)
        )

    # ---- canvas growth ----

    def _ensure_capacity(self, session: SessionContext, column_width: int, height: int) -> Image.Image:
        """Return a canvas with room for one more column, doubling when it runs out."""
        canvas: Image.Image | None = session.state.get("strip")
        needed = (session.state["columns"] + 1) * column_width

        if canvas is not None and canvas.height != height:
            # The height setting changed mid-session. Rescaling keeps the columns
            # already collected rather than discarding the night.
            canvas = canvas.resize((canvas.width, height), Image.LANCZOS)
            session.state["strip"] = canvas
            session.state["capacity"] = canvas.width

        if canvas is not None and needed <= session.state["capacity"]:
            return canvas

        capacity = max(INITIAL_CAPACITY * column_width, needed, session.state.get("capacity", 0) * 2)
        grown = Image.new("RGB", (capacity, height), (0, 0, 0))

        if canvas is not None:
            grown.paste(canvas, (0, 0))

        session.state["strip"] = grown
        session.state["capacity"] = capacity

        return grown
