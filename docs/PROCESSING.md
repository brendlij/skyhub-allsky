# Processing pipeline

Everything SkyHub builds from captures — startrails, keograms, timelapses — comes
out of one pluggable pipeline that sits behind the capture path and never blocks
it.

```
Capture upload
    ├── archive (rendered + original)
    ├── thumbnail
    ├── dashboard event  ──►  web UI
    └── pipeline.publish() ──►  [ queue ] ──►  worker
                                                 ├── Startrail
                                                 ├── Keogram
                                                 ├── Timelapse
                                                 └── …anything added later
```

The upload endpoint calls `publish()` and returns. `publish()` cannot block and
cannot raise, so no processor — however slow or broken — can delay the camera node
or fail an upload that already succeeded.

---

## The three rules

**Incremental, never a rescan.** Every processor updates as each frame lands. At
sunrise the startrail and keogram already exist; nothing walks the archive. The
timelapse keeps a manifest it appends one line to per capture, so even the encode
reads a list rather than scanning a directory.

**The queue is bounded and drops the oldest.** A processor that cannot keep up
costs frames, not memory. On a Pi an unbounded backlog of decoded images is an
out-of-memory kill that takes the camera down with it — strictly worse than a
keogram missing a column. Drops are counted and shown in Settings → Processing.

**A processor's failure is its own.** Every hook runs inside a guard. An exception
marks that processor failed for that session, records it on the session row, and
the others carry on. A broken startrail still leaves you a keogram and a timelapse.

---

## Sessions

A session is one node's captures for one archive date and one period:

```
pi5-hqcam/2026-07-27/night
```

Opened lazily by the first frame, closed when the sun crosses. The period watcher
already detects sunrise and sunset to push exposure profiles to the nodes, and the
same signal closes the session — so the boundary and the node's profile change
together, and no frame is ever attributed to a session that has been encoded.

Keyed by date and period rather than by the node's sequence id: stopping and
restarting capture mid-night should extend the night's startrail, not begin a
second one.

**Restarts resume.** Working state lives under `data/processing/…`, and each
processor reloads it in `on_session_start`. A server rebooted at 2am carries on
stacking onto four hours of trails rather than discarding them. Shutdown
deliberately does *not* finalise — a restart should resume the night, not encode
half of it.

To finalise early — after a settings change, or to recover a session whose period
change was missed while the server was down — use Products → Open sessions →
**Finalise now**, or:

```bash
curl -X POST http://skyhub.local:8000/api/processing/sessions/close \
  -H 'Content-Type: application/json' -H "X-API-Key: $KEY" \
  -d '{"node_id":"pi5-hqcam","archive_date":"2026-07-27","period":"night"}'
```

---

## What ships

### Startrail — night only

`stack = max(stack, frame)`, per pixel, per channel, via Pillow's `ImageChops.
lighter`. Stars are brighter than the sky behind them and move between frames, so
the maximum keeps every position a star has occupied and discards the sky.

Written every frame: a downscaled **live preview**, and one **build frame** for the
growth video. Written rarely: the full-resolution working stack, checkpointed
every N frames so a crash costs a few frames rather than the night.

Build frames are saved at video resolution, not sensor resolution. A night of
4056×3040 stills is tens of gigabytes for an animation that will be 1920 wide.

Products: `startrail_live`, `startrail`, `startrail_build`.

### Keogram — day and night

One strip per capture — by default the vertical centre line, which on a circular
allsky lens is horizon to horizon through the zenith — resized and appended. Cloud
reads as a vertical smear, moonrise as a brightening ramp, a front as a hard edge.

The canvas grows by doubling. Pasting into preallocated space is O(1) per frame;
rebuilding a one-column-wider strip each time would be O(n²) over a night.

Products: `keogram_live`, `keogram`.

### Timelapse — day and night

Appends a path to a manifest per frame, encodes once when the session closes.
Sessions shorter than `minimum_frames` produce nothing.

Products: `timelapse`.

---

## ffmpeg

Videos need it, images do not. ffmpeg is not pip-installable, so a server that
never had `apt install ffmpeg` run on it is a normal state and is reported rather
than crashed on:

```bash
sudo apt install ffmpeg
```

Settings → Processing shows whether it was found. Without it, startrails and
keograms work exactly as normal; timelapses and the build video report
"ffmpeg is not installed" as the product's state instead of failing silently every
night. Point at a non-PATH build with `SKYHUB_SERVER_FFMPEG_PATH`.

Codecs: `h264` (default, plays everywhere), `h265` (smaller, slower), `vp9`.

---

## Configuration

Everything is per-processor, in Settings → Processing, or over the API. Each
processor declares its own fields and the UI renders them — a processor added
later gets a working settings panel without the frontend changing.

```bash
curl -X PUT http://skyhub.local:8000/api/processing/processors/keogram \
  -H 'Content-Type: application/json' -H "X-API-Key: $KEY" \
  -d '{"enabled": true, "config": {"column_width": 4, "height": 1440}}'
```

Values are clamped to their declared range rather than rejected: a typo should
cost the setting, not the night's captures.

Server-level knobs:

| Variable | Default | |
|---|---|---|
| `SKYHUB_SERVER_PROCESSING_ENABLED` | `true` | Turn the whole pipeline off |
| `SKYHUB_SERVER_PROCESSING_QUEUE_SIZE` | `64` | Frames buffered before the oldest is dropped |
| `SKYHUB_SERVER_FFMPEG_PATH` | *(PATH)* | Explicit ffmpeg location |

---

## Storage

```
data/derived/{node}/{date}/{period}/     finished products, kept
data/processing/{node}/{date}/{period}/  working state, deleted when a session closes
```

Working state is separate so it can be wiped without losing output, and so
retention never has to tell the two apart.

---

## API

| Method | Path | |
|---|---|---|
| GET | `/api/processing/status` | Pipeline stats, ffmpeg, every processor and its fields |
| PUT | `/api/processing/processors/{name}` | Enable, prioritise, configure |
| GET | `/api/processing/products` | Filter by `node_id`, `archive_date`, `period`, `kind` |
| GET | `/api/processing/products/dates` | Dates that have products |
| GET | `/api/processing/sessions` | Recent sessions and their state |
| POST | `/api/processing/sessions/close` | Finalise now |
| GET | `/api/processing/products/{node}/{date}/{period}/{file}` | The file itself |

Product files send an `ETag` with `Cache-Control: no-cache`, so a UI polling a live
keogram gets a cheap `304` until it actually changes.

Two dashboard WebSocket events drive the UI, so nothing polls:

- `processing.products` — a product was written
- `processing.session` — a session opened, is closing, or closed

---

## Adding a processor

One module. Nothing in the capture path, the API or the UI changes.

```python
# server/app/processing/processors/meteors.py
from app.processing.base import (
    ConfigField, FrameEvent, ProductDraft, Processor, SessionContext, register_processor,
)


@register_processor
class MeteorProcessor(Processor):
    name = "meteors"
    label = "Meteor detection"
    description = "Flags frames containing a probable meteor streak."
    periods = frozenset({"night"})

    fields = (
        ConfigField("threshold", "Detection threshold", "float", 0.7, minimum=0.0, maximum=1.0),
    )

    def on_session_start(self, session: SessionContext) -> None:
        session.ensure_dirs()
        session.state["hits"] = []

    def on_frame(self, session: SessionContext, frame: FrameEvent):
        if self.looks_like_a_meteor(frame, session.config["threshold"]):
            session.state["hits"].append(frame.rendered_path)
        return ()

    def on_session_end(self, session: SessionContext):
        report = session.output_dir / "meteors.json"
        report.write_text(json.dumps([str(p) for p in session.state["hits"]]))
        return [ProductDraft(kind="meteors", path=report,
                             media_type="application/json", state="final")]
```

Then add it to `server/app/processing/processors/__init__.py`. Importing the module
is what registers it.

The contract:

| Hook | When | Budget |
|---|---|---|
| `on_session_start` | Before the first frame | Fast; reload your state here |
| `on_frame` | Every capture, in a worker thread | Well inside the capture interval |
| `on_session_end` | Once, at sunrise or sunset | Minutes are fine |

Return `ProductDraft`s and the pipeline does the rest — database rows, dashboard
events, API listing, serving, and a card in the Products view. A processor never
touches the database and never knows another processor exists.

---

## Testing

```bash
python server/tests/test_processing_pipeline.py
```

Synthesises a night with a moving star, runs it through the real pipeline, and
checks the stack really is a per-pixel maximum (every frame's star must survive),
that the keogram grows exactly one column per frame, that finalisation produces the
final products and cleans up, that a deliberately broken processor is contained,
and that a disabled one does not run.

---

## Not yet built

The spec this was built against also lists midnight jobs — 24-hour timelapses and
keograms, daily archive packages and statistics. The session machinery is where
they belong: a `period` of `"day24"` closed by a midnight trigger, using the same
hooks. The processors, the registry, the products table, the API and the UI all
take it without changes; what is missing is the midnight scheduler and the
processors themselves.

The AI processors listed as future work (meteor, aurora, cloud, lightning,
satellite and aircraft detection, weather classification) are likewise unbuilt —
the extension point above is the whole of what exists for them today.
