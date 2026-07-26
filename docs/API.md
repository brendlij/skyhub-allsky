# SkyHub API

Everything the web UI does, it does through this API, so anything here is fair game
for Home Assistant, Node-RED, n8n, a shell script or your own dashboard.

The server is a FastAPI app, so it also documents itself at runtime:

| | |
|---|---|
| Interactive docs | `http://<server>:8000/docs` |
| OpenAPI schema | `http://<server>:8000/openapi.json` |

The schema is machine-readable — point an OpenAPI client generator at it and skip
writing a client by hand. This file covers the parts a generator cannot tell you:
what the fields mean and which calls are worth making.

All examples assume `http://skyhub.local:8000`. Replace it with your server.

---

## Authentication

Authentication is **off by default**. A server with no key set answers every
request, which is the behaviour a LAN-only install has always had.

To turn it on, set an API key on the server and restart it:

```bash
SKYHUB_SERVER_API_KEY=pick-something-long-and-random python skyhub.py server
```

With a key set, every `/api/*` route and both WebSockets require it. `/health` stays
open so uptime monitoring does not need a credential.

Three ways to present the key, because clients differ in what they can send:

```bash
curl -H "X-API-Key: $KEY"            http://skyhub.local:8000/api/nodes   # preferred
curl -H "Authorization: Bearer $KEY" http://skyhub.local:8000/api/nodes   # bearer-only clients
curl "http://skyhub.local:8000/api/nodes?api_key=$KEY"                    # <img>, WebSockets
```

Prefer a header. The query parameter exists because an `<img src>` and a browser
`WebSocket` cannot set headers, but it ends up in server logs and browser history.

A missing or wrong key gets `401` with `{"detail": "Invalid or missing API key"}`;
a WebSocket handshake is closed with code `1008` before it is accepted.

### Configuring the clients

**Camera nodes** must present the same key, or they cannot connect or upload:

```bash
SKYHUB_NODE_API_KEY=$KEY python skyhub.py node --node-id roof-pi
```

**The web UI** prompts for the key the first time the server answers `401` and
stores it in that browser's `localStorage`. Clearing site data asks again.

### Sharing the image without sharing control

The API key is a full-control credential. Putting it in a public web page, a shared
dashboard or a Discord embed hands whoever reads the source the ability to change
your camera settings, stop capture and delete nodes. Don't reuse it for that.

Instead, open up the current image on its own. Two ways, both of which apply only
to `/api/captures/current` and `/api/captures/latest` (and their `?raw=` and
`?thumb=` variants):

```bash
# 1. A read-only token: anyone who has it sees the current sky, nothing else.
SKYHUB_SERVER_API_KEY=$ADMIN_KEY \
SKYHUB_SERVER_PUBLIC_CAPTURE_TOKEN=some-shareable-token \
python skyhub.py server

# 2. Wide open: no credential at all for the current image.
SKYHUB_SERVER_API_KEY=$ADMIN_KEY \
SKYHUB_SERVER_PUBLIC_CAPTURES=true \
python skyhub.py server
```

The token is presented exactly like the API key, so a client only ever has to know
about one parameter:

```html
<img src="https://sky.example.com/api/captures/current?api_key=some-shareable-token">
```

What the token or the open mode does **not** unlock: the archive, the capture list,
telemetry, settings, storage, the WebSockets, or any write at all. Every one of
those still answers `401`. Someone with it sees the newest frame and nothing else —
so if it leaks, you have shown somebody your sky.

Both settings are ignored unless `SKYHUB_SERVER_API_KEY` is set, since without a key
everything is already public.

### What this does and does not protect

The key is a single shared secret: everyone who has it can do everything, including
changing camera settings and deleting nodes. There are no user accounts and no
per-key permissions. That is proportionate for a LAN tool, but it means:

- **Do not port-forward the server.** If you want access from outside, put it behind
  a reverse proxy that terminates TLS, or reach it through your existing remote
  access (Home Assistant, Tailscale, WireGuard).
- **Use HTTPS if it leaves your network.** Over plain HTTP the key crosses the wire
  in the clear on every request.
- Rotating the key means restarting the server, every node, and re-entering it in
  each browser.

---

## Conventions

- All request and response bodies are JSON, except capture upload (multipart) and
  capture download (JPEG).
- Timestamps are ISO 8601 with a UTC offset.
- `PUT` endpoints are partial updates: send only the fields you want to change.
  `null` fields are ignored rather than clearing a value.
- `node_id` is the id the node was started with (`--node-id`), e.g. `pi5-hqcam`.
- `period` is `day` or `night`, decided from sunrise/sunset at the server's
  configured latitude and longitude.

---

## Nodes

### `GET /api/nodes`

Every node the server has ever seen, with live connection state.

```json
{
  "nodes": [
    {
      "node_id": "pi5-hqcam",
      "online": true,
      "version": "0.1.0",
      "capabilities": {"camera": "picamera2", "environment_sensor": "bme280", "heater": "gpiozero"},
      "connected_at": "2026-07-26T18:02:11.482000+00:00",
      "disconnected_at": null,
      "last_seen_at": "2026-07-26T21:47:03.119000+00:00",
      "last_message_type": "node.heartbeat"
    }
  ]
}
```

`online` is the one to alert on — it flips to `false` as soon as the WebSocket
drops. Nodes send a heartbeat every 10s by default.

### `DELETE /api/nodes/{node_id}`

Removes the node and its settings, overlays and telemetry. Captures already on disk
are kept.

---

## Captures

### `GET /api/captures/current` — just the picture

The newest frame as a JPEG, at a URL that never changes. Nothing to parse, nothing
to assemble: point an `<img>`, a Home Assistant camera or a dashboard tile straight
at it.

```
http://skyhub.local:8000/api/captures/current
```

| Query | Effect |
|---|---|
| `?node_id=pi5-hqcam` | Pick a node. Omit it on a single-node install |
| `?thumb=true` | Thumbnail instead of the full frame |
| `?raw=true` | The original, before overlays and hue correction |
| `?api_key=…` | For clients that cannot send a header, like `<img>` |

Sent with `Cache-Control: no-store`, because the URL stays the same while the
picture behind it does not. `404` until the first capture exists.

This is one of the two routes that can be opened up without handing out the full
API key — see [Sharing the image without sharing control](#sharing-the-image-without-sharing-control).

```html
<img src="http://skyhub.local:8000/api/captures/current?api_key=KEY" alt="allsky">
```

Use `/api/captures/latest` below when you also need the exposure, size or timestamp.

### `GET /api/captures/latest?node_id=…`

The most recent frame, with the metadata for that exact frame attached. This is the
endpoint to poll for a live view. `404` when nothing has been captured yet.

```json
{
  "node_id": "pi5-hqcam",
  "archive_date": "2026-07-26",
  "period": "night",
  "filename": "cap_..._picamera2_20260726_013535_49b090f1.jpg",
  "width": 4056,
  "height": 3040,
  "size_bytes": 1572864,
  "captured_at": "2026-07-26T01:35:35+00:00",
  "original_available": true,
  "thumbnail_available": true,
  "metadata": {
    "actual_exposure_ms": 50000.0,
    "actual_analogue_gain": 8.0,
    "actual_digital_gain": 1.0,
    "sensor_temperature_c": 26.5,
    "lux": 0.4,
    "mean": 0.197,
    "target_mean": 0.2,
    "mean_within_threshold": true,
    "colour_gains": [2.2, 1.8]
  }
}
```

`metadata` reports what the sensor **actually used**, not what was requested — the
two diverge whenever libcamera clamps a request or the auto controller is still
converging. It is empty if the newest file on disk is not the frame the node last
reported. Trending `actual_exposure_ms` is the cheapest way to spot an auto exposure
that is running away.

### `GET /api/captures/{node_id}/{archive_date}/{period}/{filename}`

The JPEG itself. Build the path from the four fields of a capture record.

| Query | Effect |
|---|---|
| *(none)* | The rendered capture, overlays burned in |
| `?thumb=true` | Thumbnail, generated on first request |
| `?raw=true` | The original before overlays and hue correction, if kept |

### `GET /api/captures`

Browse the archive. Query: `node_id`, `archive_date`, `period`, `limit`, `offset`.

`limit` defaults to `0`, which means **everything that matched** — pass a real limit
when querying a large archive. Returns `{"captures": [...], "count": n, "offset": n,
"total": n}`, newest first, with the same record shape as `latest`.

### `GET /api/captures/dates`

Dates that have captures, with per-date counts — for building a calendar.

```json
{"dates": [{"archive_date": "2026-07-26", "day": 163, "night": 62, "total": 225}],
 "total": 225}
```

### `POST /api/captures/upload`

How a node delivers a frame. Multipart, not something an integration should call.
Requires the API key like everything else, so an unauthenticated client cannot
inject fake captures.

---

## Camera settings

### `GET /api/nodes/{node_id}/settings`

```json
{
  "node_id": "pi5-hqcam",
  "interval_seconds": 60,
  "day_interval_seconds": 60,
  "night_interval_seconds": 60,
  "full_resolution": true,
  "width": 2028,
  "height": 1520,
  "format": "jpg",
  "day":   {"auto_exposure": true,  "exposure_ms": null,  "max_exposure_ms": 1000,
            "auto_gain": true,  "gain": null, "max_gain": 8.0,
            "auto_white_balance": true,  "wb_red": 1.0, "wb_blue": 1.0,
            "saturation": 1.0, "hue": 0.0},
  "night": {"auto_exposure": false, "exposure_ms": 50000, "max_exposure_ms": 30000,
            "auto_gain": false, "gain": 8.0,  "max_gain": 16.0,
            "auto_white_balance": false, "wb_red": 2.2, "wb_blue": 1.8,
            "saturation": 1.0, "hue": 0.0},
  "capture_enabled": true,
  "current_sequence_id": "seq_4f21…",
  "updated_at": "2026-07-26T21:40:00+00:00"
}
```

### `PUT /api/nodes/{node_id}/settings`

Flat field names, `day_`/`night_` prefixed. Partial updates are fine.

```bash
curl -X PUT http://skyhub.local:8000/api/nodes/pi5-hqcam/settings \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"night_auto_exposure": false, "night_exposure_ms": 50000, "night_interval_seconds": 60}'
```

Response: `{"settings": {...}, "node_notified": true}`. `node_notified` is `false`
when the node is offline — the change is saved and delivered when it reconnects.

Field notes worth knowing before you automate these:

- `*_exposure_ms` is used only when `*_auto_exposure` is `false`. With auto on it is
  a starting seed at most.
- `*_max_exposure_ms` / `*_max_gain` bound the auto controller and are **ignored in
  manual mode**. With no maximum the controller will run to the sensor's own ceiling
  (~670s on an HQ camera) on a dark night.
- The interval is a floor, not a guarantee. One capture costs roughly one exposure,
  plus settle frames after any control change, so a 50s exposure cannot hold a 30s
  interval.
- A running capture picks changes up before its **next** frame. No restart needed,
  but the frame in flight finishes on the old settings — which can be minutes at
  night.

---

## Capture control

### `POST /api/nodes/{node_id}/sequence/start`

Starts capturing. Empty body uses the stored settings for the current period; any
field in the body overrides it **for this sequence only** (`interval_seconds`,
`exposure_ms`, `gain`, `auto_exposure`, `auto_gain`, `width`, `height`, `format`).

```bash
curl -X POST http://skyhub.local:8000/api/nodes/pi5-hqcam/sequence/start \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{}'
```

Returns `{"status": "sent", "sequence_id": "seq_…"}`, or `status: "queued"` with the
pending message when the node is offline.

### `POST /api/nodes/{node_id}/sequence/stop`

Stops capturing.

---

## Telemetry and hardware

### `GET /api/nodes/{node_id}/environment`

Latest BME280 reading. `404` if the node has no sensor or has not reported yet.

```json
{
  "node_id": "pi5-hqcam",
  "sensor": "bme280",
  "temperature_c": 26.5,
  "humidity_percent": 26.4,
  "pressure_hpa": 1008.2,
  "dew_point_c": 6.1,
  "captured_at": "2026-07-26T01:35:30+00:00"
}
```

`dew_point_c` is the useful one for automation: when the sensor temperature
approaches it, the dome is about to fog and the heater should be on.

### `GET` / `PUT /api/nodes/{node_id}/heater`

```json
{"node_id": "pi5-hqcam", "desired_enabled": true, "actual_enabled": true,
 "driver": "gpiozero", "gpio_pin": 23}
```

`PUT` takes `{"enabled": true}`. `desired_enabled` is what was asked for,
`actual_enabled` is what the node reports the GPIO is doing — they differ while the
node is offline.

### `GET` / `PUT /api/nodes/{node_id}/devices`

Hardware wiring: sensor driver, I2C bus and address, heater driver and pin.

---

## Overlays

### `GET` / `PUT /api/nodes/{node_id}/overlays`

The text burned into saved captures.

```json
{
  "enabled": true,
  "entities": [
    {"id": "overlay-datetime", "type": "text", "label": "Date + time", "enabled": true,
     "x": 0.035, "y": 0.055, "anchor": "top-left", "font_size": 32,
     "color": "#ffffff", "background": "#000000", "background_opacity": 0.45,
     "text": "$capture.datetime"}
  ]
}
```

`x`/`y` are fractions of the image (0–1) locating the box's `anchor` corner.
`text` is a template; `$`-prefixed tokens are substituted at render time.

`PUT` returns the saved settings plus `warnings` listing any tokens that will render
as empty text — worth checking rather than wondering why a label is blank.

### `GET /api/overlays/variables?node_id=…`

Every available template variable, grouped, with its current value for that node.
This is the authoritative list — the editor builds its picker from it.

```json
{"variables": [{"token": "$exposure.time", "label": "Exposure", "group": "Exposure",
                "snippet": "Exp $exposure.time", "value": "50s", "live": true}],
 "presets": [...], "has_live_values": true}
```

---

## Storage

### `GET /api/storage`

Bytes used by captures, originals, thumbnails and the database, plus free disk.

### `GET` / `PUT /api/storage/settings`

Retention: `day_capture_enabled`, `night_capture_enabled`, `retention_days`,
`max_storage_gb`.

---

## Live events (WebSocket)

`ws://<server>:8000/ws/dashboard`

Push instead of poll — the same feed the web UI runs on. Send nothing; just read
JSON messages. With authentication on, pass `?api_key=…` (browsers) or an
`X-API-Key` header (everything else).

| `type` | Fires when | Carries |
|---|---|---|
| `capture.uploaded` | A capture has been stored and rendered | `capture` — a full record with `metadata` |
| `capture.skipped` | A capture was dropped by the retention rules | `reason`, `period` |
| `capture.state.updated` | Capture started or stopped | `capture_enabled`, `sequence_id` |
| `settings.updated` | Camera settings changed | `settings` |
| `overlay.updated` | Overlays changed | `overlays` |
| `environment.updated` | New sensor reading | `environment` |
| `heater.updated` | Heater state changed | `heater` |
| `device.settings.updated`, `device.configured` | Hardware config changed or was applied | `device_settings` |
| `storage.settings.updated` | Retention settings changed | `storage` |
| `node.updated`, `node.deleted` | A node connected, disconnected or was removed | `online` |

Every message also carries `node_id`.

```python
import asyncio, json, websockets

async def watch():
    url = "ws://skyhub.local:8000/ws/dashboard"
    async with websockets.connect(url, additional_headers={"X-API-Key": KEY}) as ws:
        async for message in ws:
            event = json.loads(message)
            if event["type"] == "capture.uploaded":
                capture = event["capture"]
                print(capture["filename"], capture["metadata"].get("actual_exposure_ms"))

asyncio.run(watch())
```

`ws://<server>:8000/ws/nodes/{node_id}` is the node protocol. It is not an
integration surface — connecting to it impersonates a camera node.

---

## Home Assistant

A camera showing the latest frame, temperature and dew point sensors, and a heater
switch. Put the key in `secrets.yaml` as `skyhub_api_key`.

```yaml
rest:
  - resource: http://skyhub.local:8000/api/captures/latest?node_id=pi5-hqcam
    headers:
      X-API-Key: !secret skyhub_api_key
    scan_interval: 60
    sensor:
      - name: SkyHub latest capture
        value_template: "{{ value_json.captured_at }}"
        json_attributes: [node_id, archive_date, period, filename, metadata]

  - resource: http://skyhub.local:8000/api/nodes/pi5-hqcam/environment
    headers:
      X-API-Key: !secret skyhub_api_key
    scan_interval: 60
    sensor:
      - name: Allsky temperature
        value_template: "{{ value_json.temperature_c }}"
        unit_of_measurement: "°C"
        device_class: temperature
      - name: Allsky dew point
        value_template: "{{ value_json.dew_point_c }}"
        unit_of_measurement: "°C"
        device_class: temperature

template:
  - sensor:
      - name: Allsky exposure
        unit_of_measurement: "s"
        state: >-
          {{ (state_attr('sensor.skyhub_latest_capture', 'metadata').get('actual_exposure_ms', 0)
              | float / 1000) | round(1) }}

camera:
  - platform: generic
    name: Allsky
    still_image_url: http://skyhub.local:8000/api/captures/current?node_id=pi5-hqcam&api_key=KEY

switch:
  - platform: rest
    name: Allsky heater
    resource: http://skyhub.local:8000/api/nodes/pi5-hqcam/heater
    state_resource: http://skyhub.local:8000/api/nodes/pi5-hqcam/heater
    is_on_template: "{{ value_json.actual_enabled }}"
    body_on: '{"enabled": true}'
    body_off: '{"enabled": false}'
    headers:
      Content-Type: application/json
      X-API-Key: !secret skyhub_api_key
    method: PUT
```

The camera points at `/api/captures/current`, so it needs no REST sensor at all —
that sensor is only there for the exposure and timestamp attributes. The generic
camera cannot send a header, so its URL carries `?api_key=`; substitute your key, or
keep it out of the YAML with an `input_text` and a template.

An automation that turns the heater on as the dome approaches dew point:

```yaml
automation:
  - alias: Allsky heater on near dew point
    trigger:
      - platform: template
        value_template: >-
          {{ (states('sensor.allsky_temperature') | float
              - states('sensor.allsky_dew_point') | float) < 2 }}
    action:
      - service: switch.turn_on
        target: {entity_id: switch.allsky_heater}
```

---

## Errors

| Status | Meaning |
|---|---|
| `400` | Malformed path or parameter |
| `401` | Missing or wrong API key |
| `404` | No such node, capture or telemetry yet |
| `422` | Body failed validation — the response names the field |

`{"detail": "..."}` carries the reason on every one of them.
