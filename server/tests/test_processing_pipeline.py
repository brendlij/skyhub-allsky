"""End-to-end checks for the processing pipeline.

Synthesises a night of frames with moving "stars", pushes them through the real
pipeline, and asserts on what comes out - that the stack really is a per-pixel
maximum, that the keogram grows one column per frame, that finalising produces the
final products, and that a broken processor is contained.

Run with:  python server/tests/test_processing_pipeline.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

_TEMP_DIR = tempfile.mkdtemp(prefix="skyhub-processing-test-")
os.environ["SKYHUB_SERVER_DATA_DIR"] = _TEMP_DIR

from datetime import datetime, timedelta, timezone  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.database import SessionLocal, create_db_tables  # noqa: E402
from app.processing import FrameEvent, registered_processors  # noqa: E402
from app.processing.base import resolve_order  # noqa: E402
from app.processing.images import open_rgb  # noqa: E402
from app.processing.pipeline import ProcessingPipeline  # noqa: E402
from app.processing.video import ffmpeg_available  # noqa: E402
from app.repositories.processing_repository import (  # noqa: E402
    DerivedProductRepository,
    ProcessingSessionRepository,
    ProcessingSettingsRepository,
)

NODE_ID = "test-node"
ARCHIVE_DATE = "2026-07-27"
PERIOD = "night"
FRAME_COUNT = 12
# Blown-out twilight frames fed at the head of the night, the way a real session
# starts: it opens at sunset, and a fixed night exposure makes those frames white.
TWILIGHT_COUNT = 3
TOTAL_FRAMES = FRAME_COUNT + TWILIGHT_COUNT
FRAME_SIZE = (320, 240)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {detail}")


def make_frame(index: int, directory: Path) -> Path:
    """A dark frame with one bright dot that walks left to right.

    A lighten stack of these must end up with every dot visible at once, which is
    a property that is easy to assert and impossible to satisfy by accident.
    """
    image = Image.new("RGB", FRAME_SIZE, (8, 8, 16))
    draw = ImageDraw.Draw(image)

    x = 10 + index * ((FRAME_SIZE[0] - 20) // FRAME_COUNT)
    draw.ellipse([x - 3, 100 - 3, x + 3, 100 + 3], fill=(255, 255, 255))

    # A band whose brightness ramps over the night, so the keogram has something
    # to show along its length.
    draw.rectangle([0, 200, FRAME_SIZE[0], 240], fill=(index * 15, index * 8, 40))

    path = directory / f"frame_{index:04d}.jpg"
    image.save(path, format="JPEG", quality=92)

    return path


def make_twilight_frame(index: int, directory: Path) -> Path:
    """A frame the way a fixed night exposure renders a sky the sun is still in.

    Near white. One of these reaching a lighten stack whites out the entire
    night, permanently, which is exactly what the dusk-to-dawn gate exists to
    prevent - so the test needs a frame that would be unmistakable if it got in.
    """
    image = Image.new("RGB", FRAME_SIZE, (250, 250, 245))

    path = directory / f"twilight_{index:04d}.jpg"
    image.save(path, format="JPEG", quality=92)

    return path


async def main() -> int:
    create_db_tables()
    settings = get_settings()

    frame_dir = Path(_TEMP_DIR) / "captures" / NODE_ID / ARCHIVE_DATE / PERIOD
    frame_dir.mkdir(parents=True, exist_ok=True)

    print("\nregistry")
    check("four processors registered", len(registered_processors()) == 4, str(sorted(registered_processors())))
    check("startrail is night only", registered_processors()["startrail"].kinds() == frozenset({"night"}))
    check("timelapse declares its ffmpeg need", registered_processors()["timelapse"].requires_ffmpeg)
    check(
        "the build video declares its dependency",
        registered_processors()["startrail_build"].depends_on == ("startrail",),
    )

    names = list(registered_processors())
    order = resolve_order(names, {name: 100 for name in names})
    check(
        "dependencies are ordered before their consumers",
        order.index("startrail") < order.index("startrail_build"),
        str(order),
    )

    print("\nconfig validation")
    keogram_class = registered_processors()["keogram"]
    coerced = keogram_class.coerce_config({"position": 99.0, "height": 5, "orientation": "sideways"})
    check("out-of-range floats are clamped", coerced["position"] == 1.0, str(coerced["position"]))
    check("out-of-range ints are clamped", coerced["height"] == 120, str(coerced["height"]))
    check("bad choices fall back", coerced["orientation"] == "vertical", coerced["orientation"])
    check("unset keys keep defaults", coerced["column_width"] == 2, str(coerced["column_width"]))

    events: list[dict] = []

    async def broadcast(message: dict) -> None:
        events.append(message)

    pipeline = ProcessingPipeline()
    await pipeline.start(broadcast=broadcast)

    print("\nfeeding a night")
    # 01:00 local at the configured site, which is inside astronomical night on
    # this date. The twilight frames below are timestamped four hours earlier, in
    # the evening the session opened, so the startrail's gate has to reject them.
    captured_at = datetime(2026, 7, 27, 23, 0, tzinfo=timezone.utc)
    twilight_at = captured_at - timedelta(hours=4)

    for index in range(TWILIGHT_COUNT):
        path = make_twilight_frame(index, frame_dir)
        pipeline.publish(
            FrameEvent(
                node_id=NODE_ID,
                archive_date=ARCHIVE_DATE,
                period=PERIOD,
                captured_at=twilight_at + timedelta(minutes=index),
                rendered_path=path,
                original_path=path,
                width=FRAME_SIZE[0],
                height=FRAME_SIZE[1],
            )
        )

    for index in range(FRAME_COUNT):
        path = make_frame(index, frame_dir)
        pipeline.publish(
            FrameEvent(
                node_id=NODE_ID,
                archive_date=ARCHIVE_DATE,
                period=PERIOD,
                captured_at=captured_at + timedelta(minutes=index),
                rendered_path=path,
                original_path=path,
                width=FRAME_SIZE[0],
                height=FRAME_SIZE[1],
            )
        )

    # Wait for the worker to drain rather than sleeping a guessed interval.
    await pipeline._queue.join()

    check("every frame was processed", pipeline.stats()["processed"] == TOTAL_FRAMES, str(pipeline.stats()))
    check("nothing was dropped", pipeline.stats()["dropped"] == 0)

    with SessionLocal() as db:
        session = ProcessingSessionRepository(db).get(f"{NODE_ID}/{ARCHIVE_DATE}/{PERIOD}")

    check("the session is open", session is not None and session.status == "open")
    check("the session counted the frames", session.frame_count == TOTAL_FRAMES, str(session.frame_count))

    print("\nlive products")
    derived = settings.derived_dir / NODE_ID / ARCHIVE_DATE / PERIOD
    live_startrail = derived / "startrail-live.jpg"
    live_keogram = derived / "keogram-live.jpg"

    check("a live startrail exists", live_startrail.is_file())
    check("a live keogram exists", live_keogram.is_file())

    with SessionLocal() as db:
        products = {record.kind: record for record in DerivedProductRepository(db).list(node_id=NODE_ID)}

    check("live products are registered", {"startrail_live", "keogram_live"} <= set(products), str(sorted(products)))
    check("live products are marked live", products["startrail_live"].state == "live")
    # The keogram takes every frame. Twilight is part of what a keogram is for -
    # the gate belongs to the startrail alone, not to the pipeline.
    check(
        "the keogram counted a column per frame",
        products["keogram_live"].frame_count == TOTAL_FRAMES,
        str(products["keogram_live"].frame_count),
    )
    check(
        "the keogram is column_width * frames wide",
        products["keogram_live"].width == TOTAL_FRAMES * 2,
        str(products["keogram_live"].width),
    )
    check(
        "the startrail counted only the frames it stacked",
        products["startrail_live"].frame_count == FRAME_COUNT,
        str(products["startrail_live"].frame_count),
    )

    print("\nthe stack really is a maximum")
    stacked = open_rgb(live_startrail)
    scale = stacked.width / FRAME_SIZE[0]
    visible = 0

    for index in range(FRAME_COUNT):
        x = int((10 + index * ((FRAME_SIZE[0] - 20) // FRAME_COUNT)) * scale)
        y = int(100 * scale)
        # Sample a small neighbourhood: the preview is resampled, so the dot's
        # centre may land a pixel either side of the computed position.
        region = stacked.crop((max(0, x - 3), max(0, y - 3), x + 4, y + 4))
        if max(pixel[0] for pixel in region.getdata()) > 180:
            visible += 1

    check("every frame's star survived the stack", visible == FRAME_COUNT, f"{visible}/{FRAME_COUNT}")

    single = open_rgb(frame_dir / "frame_0000.jpg")
    check(
        "the stack is brighter than any one frame",
        sum(sum(p) for p in stacked.getdata()) > sum(sum(p) for p in single.resize(stacked.size).getdata()),
    )

    print("\ntwilight is kept out of the stack")
    # The single check the whole gate exists for. One white frame in a per-pixel
    # maximum is unrecoverable, so if any of the three got through, the average
    # here is near 255 rather than near the night sky's own darkness.
    average = sum(sum(pixel) for pixel in stacked.getdata()) / (stacked.width * stacked.height * 3)
    check("the stack did not blow out", average < 64, f"mean channel {average:.1f}")

    print("\nbuild frames accumulate")
    build_dir = settings.processing_state_dir / NODE_ID / ARCHIVE_DATE / PERIOD / "startrail_build" / "build"
    build_frames = sorted(build_dir.glob("frame_*.jpg"))
    check(
        "a build frame per stacked capture, not per capture",
        len(build_frames) == FRAME_COUNT,
        str(len(build_frames)),
    )

    print("\ntimelapse manifest, not an archive scan")
    manifest = settings.processing_state_dir / NODE_ID / ARCHIVE_DATE / PERIOD / "timelapse" / "frames.txt"
    check("the manifest was appended per frame", manifest.is_file())
    check(
        "the manifest has one line per capture",
        len(manifest.read_text(encoding="utf-8").strip().splitlines()) == TOTAL_FRAMES,
    )

    print("\ndashboard events")
    product_events = [event for event in events if event["type"] == "processing.products"]
    check("live updates were announced", len(product_events) == TOTAL_FRAMES, str(len(product_events)))

    print("\nfinalising")
    result = await pipeline.close_session(NODE_ID, ARCHIVE_DATE, PERIOD)
    check("close reports success", result["status"] == "closed", str(result))
    check("no processor failed", not result["failed_processors"], str(result["failed_processors"]))

    check("the final startrail exists", (derived / "startrail.jpg").is_file())
    check("the final keogram exists", (derived / "keogram.jpg").is_file())

    with SessionLocal() as db:
        final = {record.kind: record for record in DerivedProductRepository(db).list(node_id=NODE_ID)}
        closed = ProcessingSessionRepository(db).get(f"{NODE_ID}/{ARCHIVE_DATE}/{PERIOD}")

    check("the session is closed", closed.status == "closed", closed.status)
    check("final products are marked final", final["startrail"].state == "final")
    check("the final keogram kept every column", final["keogram"].frame_count == TOTAL_FRAMES)
    check(
        "the final startrail counts stacked frames only",
        final["startrail"].frame_count == FRAME_COUNT,
        str(final["startrail"].frame_count),
    )
    check(
        "the skipped frames are recorded on the product",
        (final["startrail"].product_metadata or {}).get("frames_skipped") == TWILIGHT_COUNT,
        str(final["startrail"].product_metadata),
    )
    check(
        "the dark window is recorded on the product",
        "dark_from" in (final["startrail"].product_metadata or {}),
        str(sorted(final["startrail"].product_metadata or {})),
    )
    check(
        "the working counts were cleaned up",
        not (
            settings.processing_state_dir / NODE_ID / ARCHIVE_DATE / PERIOD / "startrail" / "counts.json"
        ).is_file(),
    )
    check("working state was cleaned up", not (
        settings.processing_state_dir / NODE_ID / ARCHIVE_DATE / PERIOD / "startrail" / "stack.png"
    ).is_file())

    if ffmpeg_available():
        check("a timelapse was encoded", (derived / "timelapse-night.mp4").is_file(), str(sorted(p.name for p in derived.iterdir())))
        check("the build video was encoded", (derived / "startrail-build.mp4").is_file())
        check("videos are registered", "timelapse" in final and "startrail_build" in final, str(sorted(final)))
        check("the video reports a duration", (final["timelapse"].duration_seconds or 0) > 0)
        check(
            "build frames were cleaned up after encoding",
            not sorted(build_dir.glob("frame_*.jpg")),
        )
    else:
        print("  skip  ffmpeg not installed; video checks skipped")

    print("\nclosing a session retires its live products")
    with SessionLocal() as db:
        after_close = DerivedProductRepository(db).list_for_session(f"{NODE_ID}/{ARCHIVE_DATE}/{PERIOD}")

    check(
        "nothing is still marked live",
        all(record.state != "live" for record in after_close),
        str([(r.kind, r.state) for r in after_close]),
    )
    check(
        "the redundant live copies were removed",
        not any(record.kind.endswith("_live") for record in after_close),
        str(sorted(record.kind for record in after_close)),
    )
    check(
        "the live file was deleted from disk",
        not (derived / "keogram-live.jpg").is_file(),
    )
    check("the final keogram remains", (derived / "keogram.jpg").is_file())

    print("\nthe product manager owns every product")
    with SessionLocal() as db:
        managed = {record.kind: record for record in DerivedProductRepository(db).list(node_id=NODE_ID)}

    check(
        "every product carries a category",
        all(record.category for record in managed.values()),
        str({kind: record.category for kind, record in managed.items()}),
    )
    check("startrails are categorised together", managed["startrail"].category == "startrail")
    check(
        "the build video shares the startrail category",
        managed["startrail_build"].category == "startrail",
        managed["startrail_build"].category,
    ) if "startrail_build" in managed else None
    check("keograms have their own category", managed["keogram"].category == "keogram")
    check(
        "products are linked to their session",
        managed["startrail"].session_key == f"{NODE_ID}/{ARCHIVE_DATE}/{PERIOD}",
        str(managed["startrail"].session_key),
    )
    check("final products are versioned", managed["startrail"].version >= 1)

    print("\nvariants are derived, not served full size")
    # The keogram is 24px wide here, well under every variant target, so it
    # correctly gets none - deriving a 480px "preview" of a 24px image is waste.
    check(
        "a small product gets no pointless variants",
        managed["keogram"].preview_path is None,
        str(managed["keogram"].preview_path),
    )

    wide_dir = Path(_TEMP_DIR) / "captures" / NODE_ID / "2026-08-01" / PERIOD
    wide_dir.mkdir(parents=True, exist_ok=True)
    wide_pipeline = ProcessingPipeline()
    await wide_pipeline.start(broadcast=broadcast)

    # A frame big enough that the web and preview variants are worth deriving.
    for index in range(3):
        big = Image.new("RGB", (2400, 1800), (10, 10, 20))
        ImageDraw.Draw(big).ellipse([100 + index * 300, 800, 140 + index * 300, 840], fill=(255, 255, 255))
        big_path = wide_dir / f"big_{index}.jpg"
        big.save(big_path, format="JPEG", quality=88)

        wide_pipeline.publish(
            FrameEvent(
                node_id=NODE_ID,
                archive_date="2026-08-01",
                period=PERIOD,
                captured_at=captured_at + timedelta(days=5, minutes=index),
                rendered_path=big_path,
                width=2400,
                height=1800,
            )
        )

    await wide_pipeline._queue.join()
    await wide_pipeline.close_session(NODE_ID, "2026-08-01", PERIOD)

    with SessionLocal() as db:
        wide = {
            record.kind: record
            for record in DerivedProductRepository(db).list(node_id=NODE_ID, archive_date="2026-08-01")
        }

    startrail = wide.get("startrail")
    check("a large product gets a web variant", startrail and startrail.web_path, str(startrail.web_path if startrail else None))
    check("a large product gets a preview variant", startrail and startrail.preview_path)

    if startrail and startrail.preview_path:
        preview_file = settings.derived_dir / startrail.preview_path
        check("the preview file exists", preview_file.is_file())
        check(
            "the preview is smaller than the original",
            preview_file.stat().st_size < (settings.derived_dir / startrail.relative_path).stat().st_size,
        )
        check("the preview is 480px wide", open_rgb(preview_file).width == 480, str(open_rgb(preview_file).width))

    print("\nambient metadata is attached without processors collecting it")
    meta = (startrail.product_metadata or {}) if startrail else {}
    check("the node is recorded", meta.get("node_id") == NODE_ID, str(meta))
    check("the session is time-bounded", "first_frame_at" in meta and "last_frame_at" in meta, str(sorted(meta)))
    check("the source resolution is recorded", meta.get("source_resolution") == "2400x1800", str(meta.get("source_resolution")))

    print("\nmetadata is queryable")
    with SessionLocal() as db:
        found = DerivedProductRepository(db).list(node_id=NODE_ID, metadata_key="source_resolution")
        exact = DerivedProductRepository(db).list(
            node_id=NODE_ID, metadata_key="source_resolution", metadata_value="2400x1800"
        )
        missing = DerivedProductRepository(db).list(node_id=NODE_ID, metadata_key="no_such_key")
        by_category = DerivedProductRepository(db).list(node_id=NODE_ID, category="keogram")

    check("a metadata key can be searched", len(found) > 0, str(len(found)))
    check("a metadata value can be matched", len(exact) > 0, str(len(exact)))
    check("an absent key matches nothing", len(missing) == 0, str(len(missing)))
    check("products can be filtered by category", all(p.category == "keogram" for p in by_category))

    print("\nprogress is reported")
    progress_events = [event for event in events if event["type"] == "processing.progress"]
    check("progress was published", len(progress_events) > 0, str(len(progress_events)))
    stages = {event["stage"] for event in progress_events}
    check("running was reported", "running" in stages, str(sorted(stages)))
    check("completion was reported", "completed" in stages, str(sorted(stages)))

    if ffmpeg_available():
        encoding = [event for event in progress_events if event["stage"] == "encoding"]
        check("encoding progress was reported", len(encoding) > 0, str(len(encoding)))
        check(
            "encoding reported a real percentage",
            any((event.get("percent") or 0) > 0 for event in encoding),
            str([event.get("percent") for event in encoding][:6]),
        )

    await wide_pipeline.stop()

    print("\nmanual sessions")
    manual_pipeline = ProcessingPipeline()
    await manual_pipeline.start(broadcast=broadcast)

    opened = await manual_pipeline.open_manual_session(NODE_ID, "2026-08-02", "night", "focus test")
    check("a manual session opens", opened["status"] == "open", str(opened))

    with SessionLocal() as db:
        manual_record = ProcessingSessionRepository(db).get(f"{NODE_ID}/2026-08-02/night")

    check("it is marked manual", manual_record.session_kind == "manual", str(manual_record.session_kind))
    check("its label is kept", manual_record.label == "focus test", str(manual_record.label))
    check(
        "solar sessions are still marked solar",
        session.session_kind == "solar",
        str(session.session_kind),
    ) if hasattr(session, "session_kind") else None

    await manual_pipeline.stop()

    print("\nretention")
    from app.processing.retention import RetentionRepository, apply_retention

    # Genuinely old, so a keep_days rule has something to bite on. The dates the
    # rest of the test uses are today's, which nothing should ever expire.
    old_date = "2020-01-01"
    old_dir = Path(_TEMP_DIR) / "captures" / NODE_ID / old_date / PERIOD
    old_dir.mkdir(parents=True, exist_ok=True)

    old_pipeline = ProcessingPipeline()
    await old_pipeline.start(broadcast=broadcast)

    for index in range(3):
        old_pipeline.publish(
            FrameEvent(
                node_id=NODE_ID,
                archive_date=old_date,
                period=PERIOD,
                captured_at=datetime(2020, 1, 1, 22, index, tzinfo=timezone.utc),
                rendered_path=make_frame(index, old_dir),
            )
        )

    await old_pipeline._queue.join()
    await old_pipeline.close_session(NODE_ID, old_date, PERIOD)
    await old_pipeline.stop()

    old_keogram = settings.derived_dir / NODE_ID / old_date / PERIOD / "keogram.jpg"
    check("the aged session produced a keogram", old_keogram.is_file())

    swept = apply_retention(dry_run=True)
    check("nothing is deleted without a policy", swept["removed"] == 0, str(swept))

    with SessionLocal() as db:
        RetentionRepository(db).upsert("global", "keogram", {"keep_days": 30})

    dry = apply_retention(dry_run=True)
    check("a policy finds the expired product", dry["removed"] > 0, str(dry))
    check("a dry run deletes nothing", dry["dry_run"] is True)
    check("the dry run left the file alone", old_keogram.is_file())

    real = apply_retention(dry_run=False)
    check("a real sweep removes it", real["removed"] > 0, str(real))
    check("the expired file is gone from disk", not old_keogram.is_file())

    with SessionLocal() as db:
        remaining = DerivedProductRepository(db).list(node_id=NODE_ID, category="keogram")
        recent_kept = [record for record in remaining if record.archive_date >= ARCHIVE_DATE]
        startrails = DerivedProductRepository(db).list(node_id=NODE_ID, category="startrail")

    check("recent keograms survive the same rule", len(recent_kept) > 0, str(len(recent_kept)))
    check("the aged row is gone", all(r.archive_date != old_date for r in remaining))
    check("a category without a rule is untouched", len(startrails) > 0, str(len(startrails)))
    check(
        "the aged startrail survived, having no rule",
        (settings.derived_dir / NODE_ID / old_date / PERIOD / "startrail.jpg").is_file(),
    )

    print("\na broken processor is contained")
    broken_pipeline = ProcessingPipeline()
    await broken_pipeline.start(broadcast=broadcast)

    startrail_class = registered_processors()["startrail"]
    original_hook = startrail_class.on_frame

    def explode(self, session, frame):
        raise RuntimeError("deliberate failure")

    startrail_class.on_frame = explode

    try:
        broken_date = "2026-07-28"
        broken_dir = Path(_TEMP_DIR) / "captures" / NODE_ID / broken_date / PERIOD
        broken_dir.mkdir(parents=True, exist_ok=True)

        for index in range(3):
            broken_pipeline.publish(
                FrameEvent(
                    node_id=NODE_ID,
                    archive_date=broken_date,
                    period=PERIOD,
                    captured_at=captured_at + timedelta(hours=24, minutes=index),
                    rendered_path=make_frame(index, broken_dir),
                )
            )

        await broken_pipeline._queue.join()

        broken_derived = settings.derived_dir / NODE_ID / broken_date / PERIOD
        check("the failing processor produced nothing", not (broken_derived / "startrail-live.jpg").is_file())
        check("the other processors carried on", (broken_derived / "keogram-live.jpg").is_file())

        with SessionLocal() as db:
            broken_session = ProcessingSessionRepository(db).get(f"{NODE_ID}/{broken_date}/{PERIOD}")

        check(
            "the failure was recorded on the session",
            "startrail" in (broken_session.processor_state or {}),
            str(broken_session.processor_state),
        )

        finished = await broken_pipeline.close_session(NODE_ID, broken_date, PERIOD)
        check("the session still closes", finished["status"] == "closed", str(finished))
        check("the keogram still finalised", (broken_derived / "keogram.jpg").is_file())

    finally:
        startrail_class.on_frame = original_hook
        await broken_pipeline.stop()

    print("\ndisabling a processor")
    with SessionLocal() as db:
        ProcessingSettingsRepository(db).update(registered_processors()["keogram"], {"enabled": False})

    disabled_pipeline = ProcessingPipeline()
    await disabled_pipeline.start(broadcast=broadcast)

    off_date = "2026-07-29"
    off_dir = Path(_TEMP_DIR) / "captures" / NODE_ID / off_date / PERIOD
    off_dir.mkdir(parents=True, exist_ok=True)

    disabled_pipeline.publish(
        FrameEvent(
            node_id=NODE_ID,
            archive_date=off_date,
            period=PERIOD,
            captured_at=captured_at + timedelta(hours=48),
            rendered_path=make_frame(0, off_dir),
        )
    )
    await disabled_pipeline._queue.join()

    off_derived = settings.derived_dir / NODE_ID / off_date / PERIOD
    check("a disabled processor does not run", not (off_derived / "keogram-live.jpg").is_file())
    check("the enabled ones still do", (off_derived / "startrail-live.jpg").is_file())

    await disabled_pipeline.stop()
    await pipeline.stop()

    print()

    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("all checks passed")
    return 0


def test_processing_pipeline():
    """pytest entry point."""
    assert asyncio.run(main()) == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
