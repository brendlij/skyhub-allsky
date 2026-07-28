import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import json
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

from uuid import uuid4
from astral import LocationInfo
from astral.sun import sun
from pydantic import BaseModel, Field
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import structlog
from PIL import Image

from app.db.database import SessionLocal, create_db_tables, get_db_session
from app.config import get_settings
from app import astro
from app.repositories.capture_storage_settings_repository import CaptureStorageSettingsRepository
from app.repositories.site_settings_repository import SiteSettingsRepository
from app.repositories.node_repository import NodeRepository
from app.repositories.node_camera_settings_repository import NodeCameraSettingsRepository
from app.repositories.node_capture_state_repository import NodeCaptureStateRepository
from app.repositories.node_device_settings_repository import NodeDeviceSettingsRepository
from app.repositories.node_environment_repository import NodeEnvironmentRepository
from app.repositories.node_heater_state_repository import NodeHeaterStateRepository
from app.repositories.node_overlay_settings_repository import NodeOverlaySettingsRepository
from app.repositories.overlay_preset_repository import OverlayPresetRepository, preset_to_dict
from app.realtime.connection_manager import ConnectionManager
from app.auth import sessions as auth_sessions, setup as auth_setup
from app.auth.routes import router as auth_router
from app.processing import FrameEvent, pipeline
from app.processing.retention import apply_retention
from app.processing.routes import router as processing_router
from app.repositories.admin_account_repository import AdminAccountRepository
from app.repositories.processing_repository import ProcessingSessionRepository
from app.security import (
    API_KEY_HEADER,
    API_KEY_QUERY,
    api_key_required,
    log_startup_state,
    path_is_node_route,
    request_has_valid_key,
    request_is_public,
    websocket_key_is_valid,
)
from app.masks import (
    apply_mask_to_file,
    delete_mask,
    mask_info,
    mask_path,
    save_mask,
)
from app.overlays import (
    overlay_presets,
    render_capture_image,
    unknown_tokens,
    variable_catalog,
    variable_values,
)

logger = structlog.get_logger()
connections = ConnectionManager()
settings = get_settings()


PERIOD_WATCH_INTERVAL_SECONDS = 60


async def period_watch_loop():
    """Push the new profile to every node when day turns to night, and back.

    Without this a node only ever learns its day/night profile when a sequence is
    started or settings are saved, so a capture started in daylight kept shooting
    with the day exposure profile - and the day mean target - all night, which
    drives the auto exposure controller straight to the sensor's exposure ceiling.
    """
    period = current_period()

    while True:
        await asyncio.sleep(PERIOD_WATCH_INTERVAL_SECONDS)

        try:
            latest_period = current_period()

            if latest_period == period:
                continue

            previous_period, period = period, latest_period
            db = SessionLocal()

            try:
                repo = NodeCameraSettingsRepository(db)

                for node_id in connections.online_node_ids():
                    camera_settings = repo.get_or_create(node_id)
                    await connections.send_to_node(node_id, config_update_message(camera_settings))
            finally:
                db.close()

            logger.info("period.changed", period=period)

            # Sunrise and sunset are exactly the session boundaries: the period
            # that just ended has no more frames coming, so this is the moment its
            # startrail, keogram and timelapse are finalised.
            await close_finished_sessions(previous_period)
        except Exception as error:
            # A failure here must not kill the watcher, or the next switchover is
            # missed as well.
            logger.warning("period.watch.failed", error=str(error))


async def close_finished_sessions(ended_period: str) -> None:
    """Finalise every open processing session for the period that just ended.

    Driven off the period watcher rather than a clock: the watcher already knows
    when the sun crossed, and using the same signal means the session boundary and
    the node's exposure profile change together, so no frame is ever attributed to
    a session that has already been encoded.

    Sessions are closed one at a time. Each one may spend minutes in ffmpeg, and
    running several at once on a Pi would leave none of them finishing.
    """
    db = SessionLocal()

    try:
        open_sessions = ProcessingSessionRepository(db).list_open()
    finally:
        db.close()

    for record in open_sessions:
        if record.period != ended_period:
            continue

        # A session someone opened by hand is theirs to close. The sun crossing
        # says nothing about a focus test or a meteor-shower run.
        if record.session_kind != "solar":
            continue

        try:
            await pipeline.close_session(record.node_id, record.archive_date, record.period)

        except Exception as error:
            # One session's encode failing must not strand the others still open.
            logger.warning(
                "processing.close_failed", session=record.session_key, error=str(error)
            )


SESSION_HOUSEKEEPING_INTERVAL_SECONDS = 3600


async def session_housekeeping_loop():
    """Clear sessions and trusted devices whose deadlines have passed.

    Expiry is enforced on every read regardless, so this is hygiene rather than a
    control - it keeps a long-lived install from accumulating dead rows.
    """
    while True:
        await asyncio.sleep(SESSION_HOUSEKEEPING_INTERVAL_SECONDS)

        db = SessionLocal()

        try:
            removed = auth_sessions.purge_expired(db)

            if removed:
                logger.info("auth.sessions.purged", count=removed)
        except Exception as error:
            logger.warning("auth.sessions.purge_failed", error=str(error))
        finally:
            db.close()

        # Derived products expire on their own rules, per category and per node.
        # Nothing is configured by default, so this is a no-op until an operator
        # sets a policy.
        try:
            await asyncio.to_thread(apply_retention)
        except Exception as error:
            logger.warning("processing.retention_failed", error=str(error))


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_tables()
    db = SessionLocal()
    try:
        offline_count = NodeRepository(db).mark_all_offline()
        logger.info("nodes.marked_offline", count=offline_count)

        # No operator yet: open the first-run wizard and print the token that
        # gates it, so only someone who can read the server's log or filesystem
        # can claim the account.
        if not AdminAccountRepository(db).exists():
            auth_setup.begin()
    finally:
        db.close()

    logger.info("database.ready")
    log_startup_state()

    # Started before the watchers: a frame arriving in the first second should be
    # processed, not dropped because the queue does not exist yet.
    await pipeline.start(broadcast=connections.broadcast_dashboard)

    watcher = asyncio.create_task(period_watch_loop())
    housekeeper = asyncio.create_task(session_housekeeping_loop())

    try:
        yield
    finally:
        watcher.cancel()
        housekeeper.cancel()
        # Drains what is queued and lets each processor flush its state, but
        # deliberately leaves sessions open: a restart resumes the night from
        # disk rather than encoding half of it.
        await pipeline.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)


def origin_is_same_site(request: Request) -> bool:
    """Reject a state-changing request that announces a foreign origin.

    Belt to SameSite=Strict's braces, and cheap. Only applied when the browser
    actually sent an Origin - curl, camera nodes and Home Assistant send none,
    and demanding one would break every non-browser client for no gain.
    """
    origin = request.headers.get("Origin")

    if not origin:
        return True

    host = request.headers.get("Host", "")

    if not host:
        return False

    return origin.split("://")[-1].casefold() == host.casefold()


@app.middleware("http")
async def authorisation_middleware(request: Request, call_next):
    """Guard every /api route in one place.

    A middleware rather than a per-route dependency so a route added later cannot
    forget it. The frontend, the docs and the static assets stay open - they are
    just a shell, and every call they make comes back through here anyway.

    Order matters. Public paths first, then the machine credential, then the human
    one, because only the last of those costs a database round trip.
    """
    path = request.url.path

    if not path.startswith("/api"):
        return await call_next(request)

    # /health, the login routes, and the read-only public capture paths.
    if request_is_public(request):
        return await call_next(request)

    # Machines: camera nodes and automation, holding the shared key.
    if request_has_valid_key(request):
        return await call_next(request)

    # Humans: a session cookie that has cleared both password and TOTP.
    db = SessionLocal()

    try:
        record = auth_sessions.load_session(db, request.cookies.get(auth_sessions.SESSION_COOKIE))

        if record is not None and record.stage == auth_sessions.STAGE_ACTIVE:
            if request.method in auth_sessions.UNSAFE_METHODS:
                if not origin_is_same_site(request):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-origin request refused."},
                    )

                if not auth_sessions.csrf_is_valid(request, record):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Missing or invalid CSRF token."},
                    )

            # Sliding idle window. Written before the route runs so a long upload
            # cannot have its own session expire underneath it.
            auth_sessions.touch_session(db, record)

            return await call_next(request)
    finally:
        db.close()

    # An install with no API key configured has always let nodes upload freely.
    # Locking that down here would take every existing camera offline on upgrade;
    # the startup log warns about it instead.
    if not api_key_required() and path_is_node_route(path):
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Sign in, or present a valid API key."},
        headers={"WWW-Authenticate": API_KEY_HEADER},
    )


def openapi_with_security():
    """Advertise the key in the schema so /docs grows an Authorize button."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=(
            "Camera nodes, captures, telemetry and overlays for a SkyHub allsky "
            "install. See docs/API.md in the repository for worked examples."
        ),
        routes=app.routes,
    )

    if api_key_required():
        schema.setdefault("components", {})["securitySchemes"] = {
            "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": API_KEY_HEADER},
            "ApiKeyQuery": {"type": "apiKey", "in": "query", "name": API_KEY_QUERY},
        }
        schema["security"] = [{"ApiKeyHeader": []}, {"ApiKeyQuery": []}]

    app.openapi_schema = schema
    return schema


app.openapi = openapi_with_security
app.include_router(auth_router)
app.include_router(processing_router)

if (settings.frontend_dist_dir / "assets").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=settings.frontend_dist_dir / "assets"),
        name="frontend-assets",
    )


@app.get("/health")
async def health():
    """Liveness, and nothing else.

    Public, so it must stay free of anything that helps someone decide whether
    this server is worth attacking: no account state, no setup state, no hint
    about which credentials are configured. The web UI asks /api/auth/status for
    what it needs, and camera nodes learn about the API key by being rejected.
    """
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def frontend_app():
    index_path = settings.frontend_dist_dir / "index.html"

    if index_path.exists():
        return FileResponse(index_path)

    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SkyHub</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #0f1115; color: #f3f5f7; }
    code { background: #171a21; padding: .2rem .35rem; border-radius: 4px; }
    a { color: #5bbcff; }
  </style>
</head>
<body>
  <h1>SkyHub</h1>
  <p>The Vue frontend has not been built yet.</p>
  <p>For development, run <code>cd frontend && npm install && npm run dev</code>.</p>
  <p>For Python-served static files, run <code>cd frontend && npm run build</code>, then restart the server.</p>
  <p><a href="/docs">API docs</a></p>
</body>
</html>
        """
    )


@app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SkyHub</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { min-height: 100vh; }
    .node.active { border-color: var(--bs-info) !important; }
    .node { text-align: left; }
    .preview img {
      width: 100%;
      max-height: 65vh;
      object-fit: contain;
      background: #050608;
    }
  </style>
</head>
<body data-bs-theme="dark" class="bg-body-tertiary">
  <nav class="navbar border-bottom bg-body">
    <div class="container-fluid">
      <span class="navbar-brand mb-0 h1">SkyHub</span>
      <a class="link-info" href="/docs">API docs</a>
    </div>
  </nav>
  <main class="container-fluid py-3">
    <div class="row g-3">
      <aside class="col-12 col-lg-3 col-xxl-2">
        <div class="card">
          <div class="card-header text-uppercase small fw-semibold text-secondary">Nodes</div>
          <div id="nodes" class="card-body row g-2"></div>
        </div>
      </aside>
      <div class="col-12 col-lg-9 col-xxl-10">
        <div class="vstack gap-3">
          <section class="card">
            <div class="card-header text-uppercase small fw-semibold text-secondary">Control</div>
            <div class="card-body">
              <div id="selected" class="text-secondary mb-3">No node selected</div>
              <div class="d-flex flex-wrap gap-2">
                <button id="refresh" class="btn btn-outline-secondary">Refresh</button>
                <button id="start" class="btn btn-primary" disabled>Start</button>
                <button id="stop" class="btn btn-danger" disabled>Stop</button>
              </div>
              <div id="message" class="text-secondary small mt-3"></div>
            </div>
          </section>
          <section class="card">
            <div class="card-header text-uppercase small fw-semibold text-secondary">Settings</div>
            <div class="card-body">
              <div class="row g-3">
                                <div class="col-6 col-xl-3">
                                    <label class="form-label" for="day_interval_seconds">Day interval seconds</label>
                                    <input id="day_interval_seconds" class="form-control" type="number" min="1">
                                </div>
                                <div class="col-6 col-xl-3">
                                    <label class="form-label" for="night_interval_seconds">Night interval seconds</label>
                                    <input id="night_interval_seconds" class="form-control" type="number" min="1">
                                </div>
                <div class="col-6 col-xl-3">
                  <label class="form-label" for="width">Width</label>
                  <input id="width" class="form-control" type="number" min="1">
                </div>
                <div class="col-6 col-xl-3">
                  <label class="form-label" for="height">Height</label>
                  <input id="height" class="form-control" type="number" min="1">
                </div>
                <div class="col-6 col-xl-3">
                  <label class="form-label" for="format">Format</label>
                  <input id="format" class="form-control">
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="form-check mt-xl-4">
                    <input id="day_auto_exposure" class="form-check-input" type="checkbox">
                    <label class="form-check-label" for="day_auto_exposure">Day auto exposure</label>
                  </div>
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <label class="form-label" for="day_exposure_ms">Day exposure ms</label>
                  <input id="day_exposure_ms" class="form-control" type="number" min="1">
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="form-check mt-xl-4">
                    <input id="day_auto_gain" class="form-check-input" type="checkbox">
                    <label class="form-check-label" for="day_auto_gain">Day auto gain</label>
                  </div>
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <label class="form-label" for="day_gain">Day gain</label>
                  <input id="day_gain" class="form-control" type="number" step="0.1" min="0">
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="form-check mt-xl-4">
                    <input id="night_auto_exposure" class="form-check-input" type="checkbox">
                    <label class="form-check-label" for="night_auto_exposure">Night auto exposure</label>
                  </div>
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <label class="form-label" for="night_exposure_ms">Night exposure ms</label>
                  <input id="night_exposure_ms" class="form-control" type="number" min="1">
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="form-check mt-xl-4">
                    <input id="night_auto_gain" class="form-check-input" type="checkbox">
                    <label class="form-check-label" for="night_auto_gain">Night auto gain</label>
                  </div>
                </div>
                <div class="col-12 col-md-6 col-xl-3">
                  <label class="form-label" for="night_gain">Night gain</label>
                  <input id="night_gain" class="form-control" type="number" step="0.1" min="0">
                </div>
              </div>
              <button id="save" class="btn btn-primary mt-3" disabled>Save Settings</button>
            </div>
          </section>
          <section class="card preview">
            <div class="card-header text-uppercase small fw-semibold text-secondary">Latest Capture</div>
            <div class="card-body">
              <div id="latestMeta" class="text-secondary small mb-2">No capture loaded</div>
              <img id="latestImage" class="img-fluid rounded border" alt="Latest capture" hidden>
            </div>
          </section>
        </div>
      </div>
    </div>
  </main>
          </div>
          <div class="actions">
            <button id="save" class="primary" disabled>Save Settings</button>
          </div>
        </div>
      </section>
      <section>
        <h2>Latest Capture</h2>
        <div class="content">
          <div id="latestMeta" class="meta">No capture loaded</div>
          <img id="latestImage" alt="Latest capture" hidden>
        </div>
      </section>
    </div>
  </main>
  <script>
    let selectedNodeId = null;

        const fields = [
            "day_interval_seconds", "night_interval_seconds", "width", "height", "format",
            "day_auto_exposure", "day_exposure_ms", "day_auto_gain", "day_gain",
            "night_auto_exposure", "night_exposure_ms", "night_auto_gain", "night_gain"
        ];

    function setMessage(text) {
      document.getElementById("message").textContent = text || "";
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
      }
      return response.json();
    }

    async function loadNodes() {
      const data = await requestJson("/api/nodes");
      const container = document.getElementById("nodes");
      container.innerHTML = "";
      const onlineNode = data.nodes.find(node => node.online);
      const selectedNode = data.nodes.find(node => node.node_id === selectedNodeId);

      if ((!selectedNodeId || !selectedNode || (!selectedNode.online && onlineNode)) && (onlineNode || data.nodes.length)) {
        selectedNodeId = (onlineNode || data.nodes[0]).node_id;
      }

      for (const node of data.nodes) {
        const nodeColumn = document.createElement("div");
        nodeColumn.className = "col-12";
        const button = document.createElement("button");
        button.className = "node btn btn-outline-secondary w-100 p-3" + (node.node_id === selectedNodeId ? " active" : "");
        button.innerHTML = `
          <div class="fw-semibold">${node.node_id}</div>
          <span class="badge ${node.online ? "text-bg-success" : "text-bg-danger"}">${node.online ? "online" : "offline"}</span>
          <div class="small text-secondary mt-1">${node.last_message_type || "no messages yet"}</div>
          ${node.online ? "" : "<div class=\\"small text-secondary\\">Click to select, delete below</div>"}
        `;
        button.onclick = () => selectNode(node.node_id);
        nodeColumn.appendChild(button);
        container.appendChild(nodeColumn);

        if (!node.online) {
          const deleteColumn = document.createElement("div");
          deleteColumn.className = "col-12";
          const deleteButton = document.createElement("button");
          deleteButton.className = "btn btn-outline-danger btn-sm w-100";
          deleteButton.textContent = `Delete ${node.node_id}`;
          deleteButton.onclick = () => deleteNode(node.node_id);
          deleteColumn.appendChild(deleteButton);
          container.appendChild(deleteColumn);
        }
      }

      if (selectedNodeId) {
        document.getElementById("selected").textContent = `Selected: ${selectedNodeId}`;
        document.getElementById("start").disabled = false;
        document.getElementById("stop").disabled = false;
        document.getElementById("save").disabled = false;
      }
    }

    async function selectNode(nodeId) {
      selectedNodeId = nodeId;
      document.getElementById("selected").textContent = `Selected: ${nodeId}`;
      document.getElementById("start").disabled = false;
      document.getElementById("stop").disabled = false;
      document.getElementById("save").disabled = false;
      await Promise.all([loadNodes(), loadSettings(), loadLatest()]);
    }

    async function deleteNode(nodeId) {
      await requestJson(`/api/nodes/${nodeId}`, { method: "DELETE" });
      if (selectedNodeId === nodeId) selectedNodeId = null;
      setMessage(`Deleted ${nodeId}`);
      await loadNodes();
    }

    async function loadSettings() {
      if (!selectedNodeId) return;
      const settings = await requestJson(`/api/nodes/${selectedNodeId}/settings`);
    document.getElementById("day_interval_seconds").value = settings.day_interval_seconds || settings.interval_seconds || "";
    document.getElementById("night_interval_seconds").value = settings.night_interval_seconds || settings.interval_seconds || "";
      document.getElementById("width").value = settings.width;
      document.getElementById("height").value = settings.height;
      document.getElementById("format").value = settings.format;
      document.getElementById("day_auto_exposure").checked = settings.day.auto_exposure;
      document.getElementById("day_exposure_ms").value = settings.day.exposure_ms || "";
      document.getElementById("day_auto_gain").checked = settings.day.auto_gain;
      document.getElementById("day_gain").value = settings.day.gain || "";
      document.getElementById("night_auto_exposure").checked = settings.night.auto_exposure;
      document.getElementById("night_exposure_ms").value = settings.night.exposure_ms || "";
      document.getElementById("night_auto_gain").checked = settings.night.auto_gain;
      document.getElementById("night_gain").value = settings.night.gain || "";
    }

    function value(id) {
      const element = document.getElementById(id);
      if (element.type === "checkbox") return element.checked;
      if (element.type === "number") return element.value === "" ? null : Number(element.value);
      return element.value;
    }

    async function saveSettings() {
      if (!selectedNodeId) return;
      const body = {};
      for (const field of fields) body[field] = value(field);
      await requestJson(`/api/nodes/${selectedNodeId}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      setMessage("Settings saved");
    }

    async function startCapture() {
      if (!selectedNodeId) return;
      const result = await requestJson(`/api/nodes/${selectedNodeId}/sequence/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      setMessage(`Started ${result.sequence_id}`);
    }

    async function stopCapture() {
      if (!selectedNodeId) return;
      await requestJson(`/api/nodes/${selectedNodeId}/sequence/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      setMessage("Stop sent");
      setTimeout(loadLatest, 1000);
    }

    async function loadLatest() {
      if (!selectedNodeId) return;
      const image = document.getElementById("latestImage");
      const meta = document.getElementById("latestMeta");
      try {
        const latest = await requestJson(`/api/captures/latest?node_id=${encodeURIComponent(selectedNodeId)}`);
        const url = `/api/captures/${latest.node_id}/${latest.archive_date}/${latest.period}/${latest.filename}`;
        image.src = `${url}?t=${Date.now()}`;
        image.hidden = false;
        meta.textContent = `${latest.archive_date}/${latest.period} - ${latest.filename} - ${latest.size_bytes} bytes`;
      } catch (error) {
        image.hidden = true;
        meta.textContent = "No captures for this node yet";
      }
    }

    async function refreshDashboard() {
      await loadNodes();

      if (selectedNodeId) {
        await Promise.all([loadSettings(), loadLatest()]);
      }
    }

    document.getElementById("refresh").onclick = () => refreshDashboard().catch(error => setMessage(error.message));
    document.getElementById("save").onclick = () => saveSettings().catch(error => setMessage(error.message));
    document.getElementById("start").onclick = () => startCapture().catch(error => setMessage(error.message));
    document.getElementById("stop").onclick = () => stopCapture().catch(error => setMessage(error.message));

    refreshDashboard().catch(error => setMessage(error.message));
    setInterval(() => {
      refreshDashboard().catch(() => {});
    }, 10000);
  </script>
</body>
</html>
"""


@app.get("/api/nodes")
async def list_nodes(db: Session = Depends(get_db_session)):
    repo = NodeRepository(db)
    nodes = repo.list_all()

    return {
        "nodes": [
            {
                "node_id": node.node_id,
                "online": node.online,
                "version": node.version,
                "capabilities": node.capabilities,
                "connected_at": node.connected_at.isoformat() if node.connected_at else None,
                "disconnected_at": node.disconnected_at.isoformat() if node.disconnected_at else None,
                "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
                "last_message_type": node.last_message_type,
            }
            for node in nodes
        ]
    }


class SiteSettingsUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    elevation_m: float | None = Field(default=None, ge=-500, le=9000)
    timezone: str | None = Field(default=None, max_length=80)


def site_settings_to_dict(site) -> dict:
    return {
        "label": site.label or "",
        "latitude": site.latitude,
        "longitude": site.longitude,
        "elevation_m": site.elevation_m,
        "timezone": site.timezone,
        "updated_at": site.updated_at.isoformat() if site.updated_at else None,
    }


def sun_times_to_dict(on_date: date | None = None) -> dict:
    """What the configured location means for tonight, for the settings page.

    Coordinates are four decimal places of abstraction: a map pin does not tell
    an operator whether the startrail will have anything to stack. Sunset and
    astronomical dusk do, so the answer is computed where the location is set
    rather than left to be discovered a night later.
    """
    night = on_date or datetime.now(astro.local_zone()).date()
    window = astro.dark_window(night)

    times = {
        "date": night.isoformat(),
        "timezone": astro.timezone_name(),
        "dark_from": window[0].isoformat() if window else None,
        "dark_until": window[1].isoformat() if window else None,
        "dark_hours": round((window[1] - window[0]).total_seconds() / 3600, 2) if window else 0.0,
    }

    try:
        today_sun = sun(astro.observer(), date=night, tzinfo=astro.local_zone())
        times["sunset"] = today_sun["sunset"].isoformat()
        times["sunrise"] = today_sun["sunrise"].isoformat()

    except Exception:
        # Polar day or night. The dark window above is the answer that matters
        # and it has already been computed.
        times["sunset"] = None
        times["sunrise"] = None

    return times


@app.get("/api/settings/site")
async def get_site_settings(db: Session = Depends(get_db_session)):
    site = SiteSettingsRepository(db).get_or_create()

    return {"site": site_settings_to_dict(site), "sun": sun_times_to_dict()}


@app.put("/api/settings/site")
async def update_site_settings(
    request: SiteSettingsUpdate,
    db: Session = Depends(get_db_session),
):
    values = request.model_dump(exclude_unset=True, exclude_none=True)

    if "timezone" in values:
        try:
            ZoneInfo(values["timezone"])

        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail=f"Unknown timezone: {values['timezone']}")

    site = SiteSettingsRepository(db).update(values)

    # Everything that asks where the camera is reads through the cache, so it has
    # to be dropped before the next capture computes a sun position.
    astro.invalidate()

    payload = {"site": site_settings_to_dict(site), "sun": sun_times_to_dict()}

    await connections.broadcast_dashboard({"type": "site.settings.updated", **payload})

    return payload


class CaptureStorageSettingsUpdate(BaseModel):
    day_capture_enabled: bool | None = None
    night_capture_enabled: bool | None = None
    retention_days: int | None = None
    max_storage_gb: float | None = None


@app.get("/api/storage")
async def storage_stats():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(settings.data_dir)
    captures_bytes = directory_size(settings.captures_dir)
    originals_bytes = directory_size(settings.originals_dir)
    thumbnails_bytes = directory_size(settings.thumbnails_dir)
    database_bytes = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    data_bytes = directory_size(settings.data_dir)
    capture_storage_bytes_value = captures_bytes + originals_bytes + thumbnails_bytes

    return {
        "data_dir": str(settings.data_dir),
        "captures_dir": str(settings.captures_dir),
        "originals_dir": str(settings.originals_dir),
        "thumbnails_dir": str(settings.thumbnails_dir),
        "database_path": str(settings.database_path),
        "data_bytes": data_bytes,
        "captures_bytes": captures_bytes,
        "originals_bytes": originals_bytes,
        "thumbnails_bytes": thumbnails_bytes,
        "capture_storage_bytes": capture_storage_bytes_value,
        "database_bytes": database_bytes,
        "other_data_bytes": max(0, data_bytes - capture_storage_bytes_value - database_bytes),
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
    }


@app.get("/api/storage/settings")
async def get_capture_storage_settings(db: Session = Depends(get_db_session)):
    storage_settings = CaptureStorageSettingsRepository(db).get_or_create()
    return capture_storage_settings_to_dict(storage_settings)


@app.put("/api/storage/settings")
async def update_capture_storage_settings(
    request: CaptureStorageSettingsUpdate,
    db: Session = Depends(get_db_session),
):
    values = request.model_dump(exclude_unset=True)

    for integer_field in ["retention_days"]:
        if values.get(integer_field) is not None and values[integer_field] <= 0:
            values[integer_field] = None

    for float_field in ["max_storage_gb"]:
        if values.get(float_field) is not None and values[float_field] <= 0:
            values[float_field] = None

    storage_settings = CaptureStorageSettingsRepository(db).update(values)
    cleanup_result = enforce_capture_retention(storage_settings)

    await connections.broadcast_dashboard(
        {
            "type": "storage.settings.updated",
            "storage_settings": capture_storage_settings_to_dict(storage_settings),
            "cleanup": cleanup_result,
        }
    )

    return {
        "storage_settings": capture_storage_settings_to_dict(storage_settings),
        "cleanup": cleanup_result,
    }


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: str, db: Session = Depends(get_db_session)):
    managed_node = connections.get_node(node_id)

    if managed_node is not None and managed_node.online:
        raise HTTPException(status_code=409, detail="Cannot delete an online node")

    settings_repo = NodeCameraSettingsRepository(db)
    node_repo = NodeRepository(db)
    settings_deleted = settings_repo.delete(node_id)
    node_deleted = node_repo.delete(node_id)
    delete_mask(node_id)

    if not node_deleted and not settings_deleted:
        raise HTTPException(status_code=404, detail="Node not found")

    await connections.broadcast_dashboard(
        {
            "type": "node.deleted",
            "node_id": node_id,
        }
    )

    return {
        "status": "deleted",
        "node_id": node_id,
        "node_deleted": node_deleted,
        "settings_deleted": settings_deleted,
    }


class SequenceStartRequest(BaseModel):
    interval_seconds: int | None = None
    full_resolution: bool | None = None
    exposure_ms: int | None = None
    gain: float | None = None
    auto_exposure: bool | None = None
    auto_gain: bool | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None


class SequenceStopRequest(BaseModel):
    sequence_id: str | None = None


class NodeCameraSettingsUpdate(BaseModel):
    interval_seconds: int | None = None
    day_interval_seconds: int | None = None
    night_interval_seconds: int | None = None
    full_resolution: bool | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None
    day_auto_exposure: bool | None = None
    day_exposure_ms: int | None = None
    day_max_exposure_ms: int | None = Field(default=None, ge=1)
    day_auto_gain: bool | None = None
    day_gain: float | None = None
    day_max_gain: float | None = Field(default=None, ge=1.0)
    night_auto_exposure: bool | None = None
    night_exposure_ms: int | None = None
    night_max_exposure_ms: int | None = Field(default=None, ge=1)
    night_auto_gain: bool | None = None
    night_gain: float | None = None
    night_max_gain: float | None = Field(default=None, ge=1.0)
    day_auto_white_balance: bool | None = None
    day_wb_red: float | None = Field(default=None, ge=0.1, le=8.0)
    day_wb_blue: float | None = Field(default=None, ge=0.1, le=8.0)
    day_saturation: float | None = Field(default=None, ge=0.0, le=4.0)
    day_hue: float | None = Field(default=None, ge=-180, le=180)
    night_auto_white_balance: bool | None = None
    night_wb_red: float | None = Field(default=None, ge=0.1, le=8.0)
    night_wb_blue: float | None = Field(default=None, ge=0.1, le=8.0)
    night_saturation: float | None = Field(default=None, ge=0.0, le=4.0)
    night_hue: float | None = Field(default=None, ge=-180, le=180)


class OverlayEntity(BaseModel):
    id: str
    type: str = "text"
    label: str | None = None
    enabled: bool = True
    x: float = 0
    y: float = 0
    anchor: str = "top-left"
    font_size: int = 96
    color: str = "#ffffff"
    background: str = "#000000"
    background_opacity: float = 0.35
    text: str | None = None


class NodeOverlaySettingsUpdate(BaseModel):
    enabled: bool | None = None
    entities: list[OverlayEntity] | None = None


# A preset is a layout, not a live overlay: no entity ids, because they are minted
# fresh every time the preset is applied to a node.
class OverlayPresetEntity(BaseModel):
    label: str | None = None
    text: str | None = None
    enabled: bool = True
    x: float = 0
    y: float = 0
    anchor: str = "top-left"
    font_size: int = 96
    color: str = "#ffffff"
    background: str = "#000000"
    background_opacity: float = 0.35


class OverlayPresetSave(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=300)
    entities: list[OverlayPresetEntity] = Field(min_length=1)
    overwrite: bool = False


class NodeHeaterStateUpdate(BaseModel):
    enabled: bool


class NodeDeviceSettingsUpdate(BaseModel):
    devices: dict


def safe_path_part(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ["-", "_", "."] else "_"
        for character in value
    ).strip("._") or "unknown"


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0

    total = 0

    for file_path in path.rglob("*"):
        if file_path.is_file():
            try:
                total += file_path.stat().st_size
            except OSError:
                logger.warning("storage.file_size_failed", path=str(file_path))

    return total


def parse_capture_datetime(parsed_metadata: dict) -> datetime:
    captured_at = parsed_metadata.get("captured_at")

    if isinstance(captured_at, str):
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            logger.warning("capture.timestamp.invalid", captured_at=captured_at)

    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("datetime.invalid", value=value)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed


def capture_location() -> LocationInfo:
    """The configured site. Set in the UI, not in the environment.

    Kept as a function rather than inlined at the call sites so that saving a new
    location takes effect on the next capture - `astro` caches the row and drops
    the cache when it is written.
    """
    return astro.location()


def capture_observer():
    return astro.observer()


def archive_period(captured_at: datetime) -> tuple[str, str]:
    local_timezone = astro.local_zone()
    local_time = captured_at.astimezone(local_timezone)
    location = capture_location()

    today_sun = sun(location.observer, date=local_time.date(), tzinfo=local_timezone)
    sunrise = today_sun["sunrise"]
    sunset = today_sun["sunset"]

    if sunrise <= local_time < sunset:
        return local_time.date().isoformat(), "day"

    if local_time >= sunset:
        return local_time.date().isoformat(), "night"

    previous_day = local_time.date()
    previous_day = previous_day.fromordinal(previous_day.toordinal() - 1)
    return previous_day.isoformat(), "night"


def camera_settings_to_dict(camera_settings) -> dict:
    day_interval_seconds = getattr(camera_settings, "day_interval_seconds", None)
    night_interval_seconds = getattr(camera_settings, "night_interval_seconds", None)

    return {
        "node_id": camera_settings.node_id,
        "interval_seconds": camera_settings.interval_seconds,
        "day_interval_seconds": day_interval_seconds or camera_settings.interval_seconds,
        "night_interval_seconds": night_interval_seconds or camera_settings.interval_seconds,
        "full_resolution": camera_settings.full_resolution,
        "width": camera_settings.width,
        "height": camera_settings.height,
        "format": camera_settings.format or "jpg",
        "day": {
            "auto_exposure": camera_settings.day_auto_exposure,
            "exposure_ms": camera_settings.day_exposure_ms,
            "max_exposure_ms": getattr(camera_settings, "day_max_exposure_ms", None),
            "auto_gain": camera_settings.day_auto_gain,
            "gain": camera_settings.day_gain,
            "max_gain": getattr(camera_settings, "day_max_gain", None),
            "auto_white_balance": camera_settings.day_auto_white_balance,
            "wb_red": camera_settings.day_wb_red,
            "wb_blue": camera_settings.day_wb_blue,
            "saturation": camera_settings.day_saturation,
            "hue": camera_settings.day_hue,
        },
        "night": {
            "auto_exposure": camera_settings.night_auto_exposure,
            "exposure_ms": camera_settings.night_exposure_ms,
            "max_exposure_ms": getattr(camera_settings, "night_max_exposure_ms", None),
            "auto_gain": camera_settings.night_auto_gain,
            "gain": camera_settings.night_gain,
            "max_gain": getattr(camera_settings, "night_max_gain", None),
            "auto_white_balance": camera_settings.night_auto_white_balance,
            "wb_red": camera_settings.night_wb_red,
            "wb_blue": camera_settings.night_wb_blue,
            "saturation": camera_settings.night_saturation,
            "hue": camera_settings.night_hue,
        },
        "capture_enabled": camera_settings.capture_enabled,
        "current_sequence_id": camera_settings.current_sequence_id,
        "updated_at": camera_settings.updated_at.isoformat() if camera_settings.updated_at else None,
    }


def overlay_settings_to_dict(overlay_settings) -> dict:
    return {
        "node_id": overlay_settings.node_id,
        "enabled": overlay_settings.enabled,
        "entities": overlay_settings.entities or [],
        "updated_at": overlay_settings.updated_at.isoformat() if overlay_settings.updated_at else None,
    }


def environment_to_dict(environment) -> dict:
    return {
        "node_id": environment.node_id,
        "sensor": environment.sensor_driver,
        "temperature_c": environment.temperature_c,
        "humidity_percent": environment.humidity_percent,
        "pressure_hpa": environment.pressure_hpa,
        "dew_point_c": environment.dew_point_c,
        "captured_at": environment.captured_at.isoformat() if environment.captured_at else None,
        "updated_at": environment.updated_at.isoformat() if environment.updated_at else None,
    }


def heater_state_to_dict(heater_state) -> dict:
    return {
        "node_id": heater_state.node_id,
        "desired_enabled": heater_state.desired_enabled,
        "actual_enabled": heater_state.actual_enabled,
        "driver": heater_state.driver,
        "gpio_pin": heater_state.gpio_pin,
        "updated_at": heater_state.updated_at.isoformat() if heater_state.updated_at else None,
    }


def device_settings_to_dict(device_settings) -> dict:
    return {
        "node_id": device_settings.node_id,
        "devices": device_settings.devices or {},
        "updated_at": device_settings.updated_at.isoformat() if device_settings.updated_at else None,
    }


def capture_storage_settings_to_dict(storage_settings) -> dict:
    return {
        "day_capture_enabled": storage_settings.day_capture_enabled,
        "night_capture_enabled": storage_settings.night_capture_enabled,
        "retention_days": storage_settings.retention_days,
        "max_storage_gb": storage_settings.max_storage_gb,
        "updated_at": storage_settings.updated_at.isoformat() if storage_settings.updated_at else None,
    }


def current_period() -> str:
    return archive_period(datetime.now(timezone.utc))[1]


def capture_settings_for_period(camera_settings, period: str) -> dict:
    prefix = "day" if period == "day" else "night"
    profile = getattr(camera_settings, prefix, None)

    def value_for(name: str):
        if profile is not None:
            return profile.get(name)

        return getattr(camera_settings, f"{prefix}_{name}", None)

    full_resolution = bool(getattr(camera_settings, "full_resolution", False))
    period_interval_seconds = getattr(camera_settings, f"{prefix}_interval_seconds", None)

    return {
        "interval_seconds": period_interval_seconds or camera_settings.interval_seconds,
        "day_interval_seconds": getattr(camera_settings, "day_interval_seconds", None),
        "night_interval_seconds": getattr(camera_settings, "night_interval_seconds", None),
        # Omitting the size is what tells the driver to use the full sensor; the
        # server cannot name that resolution because it does not know the sensor.
        "width": None if full_resolution else camera_settings.width,
        "height": None if full_resolution else camera_settings.height,
        "full_resolution": full_resolution,
        "format": camera_settings.format or "jpg",
        "period": period,
        "auto_exposure": value_for("auto_exposure"),
        "exposure_ms": value_for("exposure_ms"),
        "auto_gain": value_for("auto_gain"),
        "gain": value_for("gain"),
        # Ceilings for the node's mean-target controller. Ignored when auto is off.
        "max_exposure_ms": value_for("max_exposure_ms"),
        "max_gain": value_for("max_gain"),
        "auto_white_balance": value_for("auto_white_balance"),
        # The node takes libcamera ColourGains directly; hue is not a camera
        # control so it stays server-side and is applied when rendering.
        "colour_gains": [value_for("wb_red"), value_for("wb_blue")],
        "saturation": value_for("saturation"),
    }


def capture_hue_for_period(camera_settings, period: str) -> float:
    prefix = "day" if period == "day" else "night"
    return float(getattr(camera_settings, f"{prefix}_hue", 0.0) or 0.0)


def apply_sequence_overrides(capture_settings: dict, request: SequenceStartRequest | None) -> dict:
    if request is None:
        return capture_settings

    overrides = request.model_dump(exclude_none=True)
    return {
        **capture_settings,
        **overrides,
    }


def config_update_message(camera_settings) -> dict:
    period = current_period()
    return {
        "type": "config.update",
        "settings": camera_settings_to_dict(camera_settings),
        "current_period": period,
        "active_settings": capture_settings_for_period(camera_settings, period),
        "capture_enabled": camera_settings.capture_enabled,
        "sequence_id": camera_settings.current_sequence_id,
    }


def device_config_message(device_settings) -> dict:
    return {
        "type": "device.config",
        "settings": device_settings_to_dict(device_settings),
    }


CAPTURE_FILENAME_TIMESTAMP = re.compile(r"(\d{8})_(\d{6})")


def captured_at_from_filename(filename: str, fallback: datetime) -> datetime:
    match = CAPTURE_FILENAME_TIMESTAMP.search(filename)

    if not match:
        return fallback

    try:
        return datetime.strptime(
            f"{match.group(1)}{match.group(2)}",
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def capture_record_from_path(file_path: Path) -> dict:
    relative_path = file_path.relative_to(settings.captures_dir)
    node_id = relative_path.parts[0]
    archive_date = relative_path.parts[1]
    period = relative_path.parts[2]
    original_path = settings.originals_dir / relative_path
    thumbnail_path = settings.thumbnails_dir / relative_path
    stat_result = file_path.stat()
    width, height = image_dimensions(file_path)
    modified_at = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)

    return {
        "node_id": node_id,
        "archive_date": archive_date,
        "period": period,
        "filename": file_path.name,
        "path": str(file_path),
        "original_available": original_path.is_file(),
        "thumbnail_available": thumbnail_path.is_file(),
        "width": width,
        "height": height,
        "aspect_ratio": width / height if width and height else None,
        "size_bytes": stat_result.st_size,
        "modified_at": modified_at.isoformat(),
        # Overlay rendering and retention rewrite mtime, so the capture time baked
        # into the filename is the stable key to sort on.
        "captured_at": captured_at_from_filename(file_path.name, modified_at).isoformat(),
    }


def iter_capture_files():
    if not settings.captures_dir.exists():
        return

    for file_path in settings.captures_dir.glob("*/*/*/*"):
        if file_path.is_file():
            yield file_path


_dimension_cache: dict[tuple[str, int, int], tuple[int | None, int | None]] = {}


def image_dimensions(file_path: Path) -> tuple[int | None, int | None]:
    try:
        stat_result = file_path.stat()
    except OSError:
        return None, None

    # Listing a night means thousands of records; decoding every header each time
    # is what makes the captures view crawl.
    cache_key = (str(file_path), stat_result.st_mtime_ns, stat_result.st_size)
    cached = _dimension_cache.get(cache_key)

    if cached is not None:
        return cached

    try:
        with Image.open(file_path) as image:
            dimensions = (image.width, image.height)
    except OSError:
        logger.warning("capture.dimensions_failed", path=str(file_path))
        dimensions = (None, None)

    if len(_dimension_cache) > 20000:
        _dimension_cache.clear()

    _dimension_cache[cache_key] = dimensions
    return dimensions


def create_thumbnail(source_path: Path, thumbnail_path: Path, max_size: tuple[int, int] = (720, 720)) -> None:
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as image:
        # A square box keeps the short edge from binding on 4:3 and circular allsky
        # frames, which a landscape box shrank to roughly a third of the tile size.
        image.thumbnail(max_size, Image.LANCZOS)
        image.convert("RGB").save(thumbnail_path, format="JPEG", quality=88, optimize=True)


def capture_artifact_paths(rendered_file_path: Path) -> list[Path]:
    relative_path = rendered_file_path.relative_to(settings.captures_dir)
    return [
        rendered_file_path,
        settings.originals_dir / relative_path,
        settings.thumbnails_dir / relative_path,
    ]


def capture_artifact_size(rendered_file_path: Path) -> int:
    total = 0

    for file_path in capture_artifact_paths(rendered_file_path):
        if file_path.is_file():
            total += file_path.stat().st_size

    return total


def remove_empty_parents(start_dir: Path, stop_dir: Path) -> None:
    try:
        current = start_dir.resolve()
        stop = stop_dir.resolve()
    except FileNotFoundError:
        return

    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return

        current = current.parent


def delete_capture_artifacts(rendered_file_path: Path) -> int:
    deleted_bytes = 0

    for file_path in capture_artifact_paths(rendered_file_path):
        if not file_path.is_file():
            continue

        deleted_bytes += file_path.stat().st_size
        file_path.unlink()

        root_dir = (
            settings.captures_dir
            if settings.captures_dir in file_path.parents
            else settings.originals_dir
            if settings.originals_dir in file_path.parents
            else settings.thumbnails_dir
        )
        remove_empty_parents(file_path.parent, root_dir)

    return deleted_bytes


def capture_storage_bytes() -> int:
    return (
        directory_size(settings.captures_dir)
        + directory_size(settings.originals_dir)
        + directory_size(settings.thumbnails_dir)
    )


def enforce_capture_retention(storage_settings, protected_paths: set[Path] | None = None) -> dict:
    deleted_files = 0
    deleted_bytes = 0
    protected_paths = {path.resolve() for path in protected_paths or set()}
    rendered_files = list(iter_capture_files() or [])

    if storage_settings.retention_days:
        cutoff = datetime.now(timezone.utc).timestamp() - storage_settings.retention_days * 86400

        for file_path in rendered_files:
            if not file_path.is_file():
                continue

            if file_path.resolve() in protected_paths:
                continue

            if file_path.stat().st_mtime < cutoff:
                deleted_bytes += delete_capture_artifacts(file_path)
                deleted_files += 1

    if storage_settings.max_storage_gb:
        max_bytes = int(storage_settings.max_storage_gb * 1024 * 1024 * 1024)
        remaining_files = [
            file_path
            for file_path in iter_capture_files() or []
            if file_path.is_file()
        ]
        remaining_files.sort(key=lambda path: path.stat().st_mtime)
        total_bytes = capture_storage_bytes()

        for file_path in remaining_files:
            if total_bytes <= max_bytes:
                break

            if file_path.resolve() in protected_paths:
                continue

            removed_bytes = delete_capture_artifacts(file_path)
            deleted_bytes += removed_bytes
            total_bytes -= removed_bytes
            deleted_files += 1

    return {
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
    }


def capture_period_storage_enabled(storage_settings, period: str) -> bool:
    if period == "day":
        return bool(storage_settings.day_capture_enabled)

    if period == "night":
        return bool(storage_settings.night_capture_enabled)

    return True


@app.get("/api/nodes/{node_id}/settings")
async def get_node_settings(node_id: str, db: Session = Depends(get_db_session)):
    repo = NodeCameraSettingsRepository(db)
    camera_settings = repo.get_or_create(node_id)
    return camera_settings_to_dict(camera_settings)


@app.put("/api/nodes/{node_id}/settings")
async def update_node_settings(
    node_id: str,
    request: NodeCameraSettingsUpdate,
    db: Session = Depends(get_db_session),
):
    repo = NodeCameraSettingsRepository(db)
    camera_settings = repo.update(node_id, request.model_dump(exclude_none=True))
    settings_payload = camera_settings_to_dict(camera_settings)
    sent = await connections.send_to_node(node_id, config_update_message(camera_settings))
    await connections.broadcast_dashboard(
        {
            "type": "settings.updated",
            "node_id": node_id,
            "settings": settings_payload,
            "node_notified": sent,
        }
    )

    return {
        "settings": settings_payload,
        "node_notified": sent,
    }


def overlay_preview_values(node_id: str | None, db: Session) -> dict:
    """Resolve overlay variables the way a capture right now would.

    Pulls the last capture's per-frame metadata plus current environment, heater
    and sun/moon so the editor previews real numbers instead of invented ones.
    """
    if not node_id:
        return {}

    capture_state = NodeCaptureStateRepository(db).get(node_id)
    camera_settings = NodeCameraSettingsRepository(db).get_or_create(node_id)
    environment = NodeEnvironmentRepository(db).get(node_id)
    heater_state = NodeHeaterStateRepository(db).get(node_id)

    captured_at = getattr(capture_state, "captured_at", None) or datetime.now(timezone.utc)

    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)

    period = getattr(capture_state, "period", None) or current_period()

    context = {
        "node_id": node_id,
        "captured_at": captured_at.astimezone(astro.local_zone()),
        "period": period,
        "timezone_name": astro.timezone_name(),
        "environment": environment,
        "heater": heater_state,
        "camera_settings": camera_settings,
        "metadata": getattr(capture_state, "capture_metadata", None) or {},
        "observer": capture_observer(),
        "sequence_id": getattr(capture_state, "sequence_id", None),
        "filename": getattr(capture_state, "filename", None),
        "size_bytes": getattr(capture_state, "size_bytes", None),
        "format": getattr(capture_state, "image_format", None),
        "width": getattr(capture_state, "width", None),
        "height": getattr(capture_state, "height", None),
    }

    return variable_values(context)


@app.get("/api/overlays/variables")
async def list_overlay_variables(
    node_id: str | None = None,
    db: Session = Depends(get_db_session),
):
    live_values = overlay_preview_values(node_id, db)
    # Sun and moon resolve from the clock alone, so their presence says nothing
    # about whether real capture data is available. Report on the capture instead.
    has_capture = bool(node_id) and NodeCaptureStateRepository(db).get(node_id) is not None

    return {
        "variables": variable_catalog(live_values),
        "presets": all_overlay_presets(db),
        "node_id": node_id,
        "has_live_values": has_capture,
    }


def all_overlay_presets(db: Session) -> list[dict]:
    """Built-in layouts first, then the user's saved ones."""
    return overlay_presets() + [
        preset_to_dict(preset) for preset in OverlayPresetRepository(db).list()
    ]


@app.get("/api/overlays/presets")
async def list_overlay_presets(db: Session = Depends(get_db_session)):
    return {"presets": all_overlay_presets(db)}


@app.post("/api/overlays/presets", status_code=201)
async def create_overlay_preset(
    request: OverlayPresetSave,
    db: Session = Depends(get_db_session),
):
    repo = OverlayPresetRepository(db)
    name = request.name.strip()

    if not name:
        raise HTTPException(status_code=400, detail="A preset needs a name")

    entities = [entity.model_dump() for entity in request.entities]
    existing = repo.get_by_name(name)

    # Saving over a name is how you update a preset, but only when the client
    # said so - otherwise a repeated save would silently replace someone's layout.
    if existing is not None:
        if not request.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f'A preset named "{name}" already exists',
            )

        preset = repo.update(
            existing,
            {"description": request.description, "entities": entities},
        )

    else:
        preset = repo.create(name, entities, request.description)

    logger.info("overlay.preset_saved", preset_id=preset.id, name=preset.name)

    return preset_to_dict(preset)


@app.delete("/api/overlays/presets/{preset_id}", status_code=204)
async def delete_overlay_preset(preset_id: str, db: Session = Depends(get_db_session)):
    repo = OverlayPresetRepository(db)
    preset = repo.get(preset_id)

    if preset is None:
        # Built-in presets live in code, so there is nothing to delete either way.
        raise HTTPException(status_code=404, detail="Preset not found")

    repo.delete(preset)
    logger.info("overlay.preset_deleted", preset_id=preset_id)

    return Response(status_code=204)


@app.get("/api/nodes/{node_id}/overlays")
async def get_node_overlays(node_id: str, db: Session = Depends(get_db_session)):
    repo = NodeOverlaySettingsRepository(db)
    overlay_settings = repo.get_or_create(node_id)
    return overlay_settings_to_dict(overlay_settings)


@app.put("/api/nodes/{node_id}/overlays")
async def update_node_overlays(
    node_id: str,
    request: NodeOverlaySettingsUpdate,
    db: Session = Depends(get_db_session),
):
    repo = NodeOverlaySettingsRepository(db)
    values = request.model_dump(exclude_none=True)

    if "entities" in values:
        values["entities"] = [
            entity.model_dump() if hasattr(entity, "model_dump") else entity
            for entity in request.entities or []
        ]

    overlay_settings = repo.update(node_id, values)
    payload = overlay_settings_to_dict(overlay_settings)

    # An unrecognised token renders as empty text with no other symptom, so report
    # it back rather than letting a typo quietly blank out part of the overlay.
    warnings = []

    for entity in overlay_settings.entities or []:
        for token in unknown_tokens(entity.get("text") or ""):
            warnings.append({"entity_id": entity.get("id"), "token": token})

    payload["warnings"] = warnings

    if warnings:
        logger.warning("overlay.unknown_tokens", node_id=node_id, warnings=warnings)

    await connections.broadcast_dashboard(
        {
            "type": "overlay.updated",
            "node_id": node_id,
            "overlays": payload,
        }
    )

    return payload


@app.get("/api/nodes/{node_id}/mask")
async def get_node_mask(node_id: str):
    return mask_info(node_id)


@app.get("/api/nodes/{node_id}/mask/image", include_in_schema=False)
async def get_node_mask_image(node_id: str):
    path = mask_path(node_id)

    if not path.exists():
        raise HTTPException(status_code=404, detail="No mask for this node")

    return FileResponse(
        path,
        media_type="image/png",
        # The filename never changes, so a cached copy would survive a re-upload.
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/nodes/{node_id}/mask")
async def upload_node_mask(node_id: str, file: UploadFile = File(...)):
    data = await file.read()

    try:
        info = save_mask(node_id, data)

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    logger.info("mask.uploaded", node_id=node_id, size_bytes=info.get("size_bytes"))

    await connections.broadcast_dashboard({"type": "mask.updated", "node_id": node_id, "mask": info})

    return info


@app.delete("/api/nodes/{node_id}/mask")
async def remove_node_mask(node_id: str):
    if not delete_mask(node_id):
        raise HTTPException(status_code=404, detail="No mask for this node")

    logger.info("mask.deleted", node_id=node_id)

    info = mask_info(node_id)
    await connections.broadcast_dashboard({"type": "mask.updated", "node_id": node_id, "mask": info})

    return info


@app.get("/api/nodes/{node_id}/environment")
async def get_node_environment(node_id: str, db: Session = Depends(get_db_session)):
    environment = NodeEnvironmentRepository(db).get(node_id)

    if environment is None:
        raise HTTPException(status_code=404, detail="No environment telemetry for node")

    return environment_to_dict(environment)


@app.get("/api/nodes/{node_id}/devices")
async def get_node_device_settings(node_id: str, db: Session = Depends(get_db_session)):
    device_settings = NodeDeviceSettingsRepository(db).get_or_create(node_id)
    return device_settings_to_dict(device_settings)


@app.put("/api/nodes/{node_id}/devices")
async def update_node_device_settings(
    node_id: str,
    request: NodeDeviceSettingsUpdate,
    db: Session = Depends(get_db_session),
):
    repo = NodeDeviceSettingsRepository(db)
    device_settings = repo.update(node_id, request.devices)
    payload = device_settings_to_dict(device_settings)
    sent = await connections.send_to_node(node_id, device_config_message(device_settings))
    heater_state = NodeHeaterStateRepository(db).get_or_create(node_id)

    if sent:
        await connections.send_to_node(
            node_id,
            {
                "type": "heater.set",
                "enabled": heater_state.desired_enabled,
            },
        )

    await connections.broadcast_dashboard(
        {
            "type": "device.settings.updated",
            "node_id": node_id,
            "device_settings": payload,
            "node_notified": sent,
        }
    )

    return {
        "device_settings": payload,
        "node_notified": sent,
    }


@app.get("/api/nodes/{node_id}/heater")
async def get_node_heater_state(node_id: str, db: Session = Depends(get_db_session)):
    heater_state = NodeHeaterStateRepository(db).get_or_create(node_id)
    return heater_state_to_dict(heater_state)


@app.put("/api/nodes/{node_id}/heater")
async def update_node_heater_state(
    node_id: str,
    request: NodeHeaterStateUpdate,
    db: Session = Depends(get_db_session),
):
    repo = NodeHeaterStateRepository(db)
    heater_state = repo.set_desired(node_id, request.enabled)
    message = {
        "type": "heater.set",
        "enabled": heater_state.desired_enabled,
    }
    sent = await connections.send_to_node(node_id, message)
    payload = heater_state_to_dict(heater_state)
    await connections.broadcast_dashboard(
        {
            "type": "heater.updated",
            "node_id": node_id,
            "heater": payload,
            "node_notified": sent,
        }
    )

    return {
        "heater": payload,
        "node_notified": sent,
    }


@app.post("/api/nodes/{node_id}/sequence/start")
async def start_sequence(
    node_id: str,
    request: SequenceStartRequest | None = None,
    db: Session = Depends(get_db_session),
):
    sequence_id = f"seq_{uuid4().hex}"
    settings_repo = NodeCameraSettingsRepository(db)
    camera_settings = settings_repo.update(
        node_id,
        {
            "capture_enabled": True,
            "current_sequence_id": sequence_id,
        },
    )
    period = current_period()
    overrides = request.model_dump(exclude_none=True) if request is not None else {}
    effective_settings = apply_sequence_overrides(
        capture_settings_for_period(camera_settings, period),
        request,
    )

    message = {
        "type": "sequence.start",
        "sequence_id": sequence_id,
        "settings": effective_settings,
        # Sent separately as well: the node keeps only these for the lifetime of
        # the sequence and lets everything else follow the node's live config, so
        # a settings change or the day/night switchover reaches a running capture.
        "overrides": overrides,
    }

    sent = await connections.send_to_node(node_id, message)
    await connections.broadcast_dashboard(
        {
            "type": "capture.state.updated",
            "node_id": node_id,
            "capture_enabled": True,
            "sequence_id": sequence_id,
            "sent": sent,
        }
    )

    if not sent:
        return {
            "status": "queued",
            "reason": "node_not_connected",
            "node_id": node_id,
            "sequence_id": sequence_id,
            "message": message,
        }

    return {
        "status": "sent",
        "node_id": node_id,
        "sequence_id": sequence_id,
        "message": message,
    }


@app.post("/api/nodes/{node_id}/sequence/stop")
async def stop_sequence(
    node_id: str,
    request: SequenceStopRequest | None = None,
    db: Session = Depends(get_db_session),
):
    settings_repo = NodeCameraSettingsRepository(db)
    existing_settings = settings_repo.get_or_create(node_id)
    sequence_id = request.sequence_id if request else existing_settings.current_sequence_id
    settings_repo.update(
        node_id,
        {
            "capture_enabled": False,
            "current_sequence_id": None,
        },
    )
    message = {
        "type": "sequence.stop",
        "sequence_id": sequence_id,
    }

    sent = await connections.send_to_node(node_id, message)
    await connections.broadcast_dashboard(
        {
            "type": "capture.state.updated",
            "node_id": node_id,
            "capture_enabled": False,
            "sequence_id": sequence_id,
            "sent": sent,
        }
    )

    if not sent:
        return {
            "status": "queued",
            "reason": "node_not_connected",
            "node_id": node_id,
            "sequence_id": sequence_id,
            "message": message,
        }

    return {
        "status": "sent",
        "node_id": node_id,
        "sequence_id": message["sequence_id"],
        "message": message,
    }


@app.get("/api/captures/dates")
async def list_capture_dates(node_id: str | None = None):
    groups: dict[str, dict] = {}

    # Walking names only (no image decode) keeps this cheap enough to cover the
    # whole archive, so the date list always shows true counts.
    for file_path in iter_capture_files() or []:
        relative_path = file_path.relative_to(settings.captures_dir)
        file_node_id, archive_date, period = relative_path.parts[0:3]

        if node_id is not None and file_node_id != node_id:
            continue

        group = groups.setdefault(
            archive_date,
            {"archive_date": archive_date, "day": 0, "night": 0, "total": 0},
        )

        if period in group:
            group[period] += 1
        else:
            group[period] = 1

        group["total"] += 1

    dates = sorted(groups.values(), key=lambda group: group["archive_date"], reverse=True)

    return {
        "dates": dates,
        "total": sum(group["total"] for group in dates),
    }


@app.get("/api/captures")
async def list_captures(
    node_id: str | None = None,
    archive_date: str | None = None,
    period: str | None = None,
    limit: int = 0,
    offset: int = 0,
):
    records = []

    for file_path in iter_capture_files() or []:
        relative_path = file_path.relative_to(settings.captures_dir)
        file_node_id, file_archive_date, file_period = relative_path.parts[0:3]

        # Filter on the path before building the record; the record does file I/O.
        if node_id is not None and file_node_id != node_id:
            continue

        if archive_date is not None and file_archive_date != archive_date:
            continue

        if period is not None and file_period != period:
            continue

        records.append(capture_record_from_path(file_path))

    records.sort(key=lambda record: record["captured_at"], reverse=True)
    total = len(records)
    offset = max(0, offset)
    # limit=0 means "everything that matched" so a selected night is never
    # truncated by a budget shared with every other night in the archive.
    page = records[offset:] if limit <= 0 else records[offset:offset + limit]

    return {
        "captures": page,
        "count": len(page),
        "offset": offset,
        "total": total,
    }


def latest_capture_record(node_id: str | None = None) -> dict | None:
    records = []

    for file_path in iter_capture_files() or []:
        record = capture_record_from_path(file_path)

        if node_id is not None and record["node_id"] != node_id:
            continue

        records.append(record)

    if not records:
        return None

    records.sort(key=lambda record: record["captured_at"], reverse=True)

    return records[0]


@app.get("/api/captures/current")
async def current_capture_image(
    request: Request,
    node_id: str | None = None,
    raw: bool = False,
    thumb: bool = False,
):
    """The newest frame as an image, at a URL that never changes.

    /api/captures/latest describes the frame in JSON, which means a viewer has to
    make two requests and assemble a path before it can show anything. Anything
    that just wants a picture - an <img>, a Home Assistant camera, a dashboard
    tile - wants one stable URL instead.

    Conditional: a client that polls gets 304 and no body until the frame actually
    changes, so checking every few seconds costs a couple of hundred bytes rather
    than re-sending two megabytes of sky.
    """
    latest = latest_capture_record(node_id)

    if latest is None:
        raise HTTPException(status_code=404, detail="No captures found")

    # The filename identifies the frame - a new capture is always a new name - so
    # it doubles as the validator without hashing or stat-ing anything.
    variant = "raw" if raw else "thumb" if thumb else "rendered"
    etag = f'W/"{latest["filename"]}-{variant}"'
    if_none_match = request.headers.get("if-none-match") or ""

    # no-cache rather than no-store: the client may keep the frame, it just has to
    # revalidate before reusing it. no-store would forbid the copy that makes a 304
    # possible, and the URL outlives the picture behind it.
    headers = {"ETag": etag, "Cache-Control": "no-cache"}

    if etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers=headers)

    response = capture_file_response(
        latest["node_id"],
        latest["archive_date"],
        latest["period"],
        latest["filename"],
        raw=raw,
        thumb=thumb,
    )
    response.headers.update(headers)

    return response


@app.get("/api/captures/latest")
async def latest_capture(
    node_id: str | None = None,
    db: Session = Depends(get_db_session),
):
    latest = latest_capture_record(node_id)

    if latest is None:
        raise HTTPException(status_code=404, detail="No captures found")

    # Per-frame metadata lives in node_capture_state rather than on disk. Attach it
    # when it belongs to this exact frame, so the dashboard reports the exposure the
    # sensor actually used instead of the value that was requested.
    capture_state = NodeCaptureStateRepository(db).get(latest["node_id"])

    if capture_state is not None and capture_state.filename == latest["filename"]:
        latest["metadata"] = capture_state.capture_metadata or {}
    else:
        latest["metadata"] = {}

    return latest


def capture_file_response(
    node_id: str,
    archive_date: str,
    period: str,
    filename: str,
    *,
    raw: bool = False,
    thumb: bool = False,
) -> FileResponse:
    safe_node_id = safe_path_part(node_id)
    safe_archive_date = safe_path_part(archive_date)
    safe_period = safe_path_part(period)
    safe_filename = safe_path_part(filename)
    rendered_file_path = settings.captures_dir / safe_node_id / safe_archive_date / safe_period / safe_filename
    original_file_path = settings.originals_dir / safe_node_id / safe_archive_date / safe_period / safe_filename
    thumbnail_file_path = settings.thumbnails_dir / safe_node_id / safe_archive_date / safe_period / safe_filename

    if thumb and thumbnail_file_path.is_file():
        file_path = thumbnail_file_path
        root_dir = settings.thumbnails_dir
    elif thumb and rendered_file_path.is_file():
        create_thumbnail(rendered_file_path, thumbnail_file_path)
        file_path = thumbnail_file_path
        root_dir = settings.thumbnails_dir
    elif raw and original_file_path.is_file():
        file_path = original_file_path
        root_dir = settings.originals_dir
    else:
        file_path = rendered_file_path
        root_dir = settings.captures_dir

    try:
        resolved_root_dir = root_dir.resolve()
        resolved_file_path = file_path.resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Capture not found")

    if not resolved_file_path.is_relative_to(resolved_root_dir):
        raise HTTPException(status_code=400, detail="Invalid capture path")

    if not resolved_file_path.is_file():
        raise HTTPException(status_code=404, detail="Capture not found")

    return FileResponse(
        resolved_file_path,
        media_type="image/jpeg",
        filename=resolved_file_path.name,
        # Starlette pairs a filename with "attachment" unless told otherwise, which
        # makes a browser download the capture instead of showing it. Inline keeps
        # the nice filename for "save image as" while the URL still just displays.
        content_disposition_type="inline",
    )


@app.get("/api/captures/{node_id}/{archive_date}/{period}/{filename}")
async def get_capture_file(
    node_id: str,
    archive_date: str,
    period: str,
    filename: str,
    raw: bool = False,
    thumb: bool = False,
):
    return capture_file_response(node_id, archive_date, period, filename, raw=raw, thumb=thumb)


@app.post("/api/captures/upload")
async def upload_capture(
    node_id: str = Form(...),
    sequence_id: str | None = Form(default=None),
    format: str = Form(default="jpg"),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    metadata: str = Form(default="{}"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db_session),
):
    try:
        parsed_metadata = json.loads(metadata)
    except json.JSONDecodeError:
        parsed_metadata = {"raw": metadata}

    upload_node_id = safe_path_part(node_id)
    upload_format = safe_path_part(format.lower())
    original_name = safe_path_part(Path(file.filename or f"capture.{upload_format}").name)
    capture_id = f"cap_{uuid4().hex}"
    captured_at = parse_capture_datetime(parsed_metadata)
    archive_date, period = archive_period(captured_at)
    storage_settings = CaptureStorageSettingsRepository(db).get_or_create()

    if not capture_period_storage_enabled(storage_settings, period):
        logger.info(
            "capture.skipped.storage_disabled",
            node_id=node_id,
            sequence_id=sequence_id,
            archive_date=archive_date,
            period=period,
        )
        await connections.broadcast_dashboard(
            {
                "type": "capture.skipped",
                "node_id": node_id,
                "reason": "storage_disabled",
                "period": period,
                "archive_date": archive_date,
            }
        )

        return {
            "status": "skipped",
            "reason": "storage_disabled",
            "node_id": node_id,
            "sequence_id": sequence_id,
            "archive_date": archive_date,
            "period": period,
        }

    filename = f"{capture_id}_{original_name}"
    capture_dir = settings.captures_dir / upload_node_id / archive_date / period
    output_path = capture_dir / filename
    original_dir = settings.originals_dir / upload_node_id / archive_date / period
    original_path = original_dir / filename
    thumbnail_dir = settings.thumbnails_dir / upload_node_id / archive_date / period
    thumbnail_path = thumbnail_dir / filename

    capture_dir.mkdir(parents=True, exist_ok=True)
    original_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    with output_path.open("wb") as output_file:
        shutil.copyfileobj(file.file, output_file)

    # Before the original is filed away, so the mask covers the raw copy too. It
    # is one extra encode, and only when the node actually has a mask.
    try:
        apply_mask_to_file(upload_node_id, output_path)

    except OSError as error:
        # A broken mask must not cost the frame that just came off the camera.
        logger.warning("capture.mask_failed", node_id=node_id, error=str(error))

    shutil.copy2(output_path, original_path)

    overlay_settings = NodeOverlaySettingsRepository(db).get_or_create(upload_node_id)
    environment = NodeEnvironmentRepository(db).get(upload_node_id)
    heater_state = NodeHeaterStateRepository(db).get(upload_node_id)
    camera_settings = NodeCameraSettingsRepository(db).get_or_create(upload_node_id)
    render_capture_image(
        output_path,
        overlay_settings,
        node_id=node_id,
        captured_at=captured_at,
        period=period,
        timezone_name=astro.timezone_name(),
        environment=environment,
        heater=heater_state,
        camera_settings=camera_settings,
        hue_shift=capture_hue_for_period(camera_settings, period),
        metadata=parsed_metadata,
        observer=capture_observer(),
        sequence_id=sequence_id,
        size_bytes=output_path.stat().st_size,
        image_format=upload_format,
    )
    create_thumbnail(output_path, thumbnail_path)
    capture_record = capture_record_from_path(output_path)
    NodeCaptureStateRepository(db).record(
        upload_node_id,
        {
            "sequence_id": sequence_id,
            "filename": capture_record["filename"],
            "archive_date": archive_date,
            "period": period,
            "image_format": upload_format,
            "width": capture_record["width"],
            "height": capture_record["height"],
            "size_bytes": capture_record["size_bytes"],
            "capture_metadata": parsed_metadata,
            "captured_at": captured_at,
        },
    )
    cleanup_result = enforce_capture_retention(storage_settings, protected_paths={output_path})

    # Hand the frame to the processors and move on. publish() is non-blocking and
    # swallows its own failures, so nothing downstream of here can delay the
    # response to the camera node or fail an upload that already succeeded.
    pipeline.publish(
        FrameEvent(
            node_id=upload_node_id,
            archive_date=archive_date,
            period=period,
            captured_at=captured_at,
            rendered_path=output_path,
            original_path=original_path,
            thumbnail_path=thumbnail_path,
            sequence_id=sequence_id,
            width=capture_record["width"],
            height=capture_record["height"],
            metadata=parsed_metadata,
        )
    )

    logger.info(
        "capture.uploaded",
        node_id=node_id,
        sequence_id=sequence_id,
        capture_id=capture_id,
        archive_date=archive_date,
        period=period,
        path=str(output_path),
        size_bytes=capture_record["size_bytes"],
        cleanup=cleanup_result,
    )

    await connections.broadcast_dashboard(
        {
            "type": "capture.uploaded",
            "node_id": node_id,
            # Carry the frame metadata so a live update shows the same exposure
            # details as a page load, which reads it back from capture state.
            "capture": {**capture_record, "metadata": parsed_metadata},
            "cleanup": cleanup_result,
        }
    )

    return {
        "status": "stored",
        "capture_id": capture_id,
        "node_id": node_id,
        "sequence_id": sequence_id,
        "path": capture_record["path"],
        "filename": capture_record["filename"],
        "archive_date": capture_record["archive_date"],
        "period": capture_record["period"],
        "format": upload_format,
        "width": capture_record["width"],
        "height": capture_record["height"],
        "aspect_ratio": capture_record["aspect_ratio"],
        "metadata": parsed_metadata,
        "size_bytes": capture_record["size_bytes"],
        "cleanup": cleanup_result,
    }


async def node_socket_is_authorised(websocket: WebSocket) -> bool:
    """A camera node's handshake. API key only - a node has no cookie jar.

    Unauthenticated when no key is configured, which is the behaviour every
    existing install already runs with; the startup log warns about it.
    """
    if not api_key_required():
        return True

    if await websocket_key_is_valid(websocket):
        return True

    logger.warning("websocket.unauthorised", path=websocket.url.path)
    # Closing before accept() matters: accept-then-close looks to the client like
    # a working connection that mysteriously went quiet.
    await websocket.close(code=1008, reason="Invalid or missing API key")

    return False


async def dashboard_socket_is_authorised(websocket: WebSocket) -> bool:
    """The browser's live feed. A session cookie, or a key for a headless client.

    The cookie rides along on the handshake by itself, so a logged-in UI needs no
    key in the query string - which is how the token used to end up in server
    logs and browser history.
    """
    db = SessionLocal()

    try:
        record = auth_sessions.load_session(
            db, websocket.cookies.get(auth_sessions.SESSION_COOKIE)
        )

        if record is not None and record.stage == auth_sessions.STAGE_ACTIVE:
            return True
    finally:
        db.close()

    if await websocket_key_is_valid(websocket):
        return True

    logger.warning("websocket.unauthorised", path=websocket.url.path)
    await websocket.close(code=1008, reason="Sign in, or present a valid API key")

    return False


@app.websocket("/ws/nodes/{node_id}")
async def node_websocket(websocket: WebSocket, node_id: str):
    if not await node_socket_is_authorised(websocket):
        return

    await connections.connect(node_id, websocket)

    db = SessionLocal()
    repo = NodeRepository(db)

    try:
        repo.mark_online(node_id)
        logger.info("node.connected", node_id=node_id)
        await connections.broadcast_dashboard(
            {
                "type": "node.updated",
                "node_id": node_id,
                "online": True,
            }
        )

        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            version = None
            capabilities = None

            if message_type == "node.hello":
                version = message.get("version")
                capabilities = message.get("capabilities", {})

            repo.update_last_seen(
                node_id=node_id,
                message_type=message_type,
                version=version,
                capabilities=capabilities,
            )

            connections.update_last_seen(
                node_id=node_id,
                message_type=message_type,
                metadata={
                    "version": version,
                    "capabilities": capabilities,
                }
                if message_type == "node.hello"
                else None,
            )

            logger.info(
                "node.message.received",
                node_id=node_id,
                message_type=message_type,
            )

            if message_type == "node.hello":
                settings_repo = NodeCameraSettingsRepository(db)
                camera_settings = settings_repo.get_or_create(node_id)
                device_settings = NodeDeviceSettingsRepository(db).get_or_create(node_id)
                heater_state = NodeHeaterStateRepository(db).get_or_create(node_id)
                await websocket.send_json(config_update_message(camera_settings))
                await websocket.send_json(device_config_message(device_settings))
                await websocket.send_json(
                    {
                        "type": "heater.set",
                        "enabled": heater_state.desired_enabled,
                    }
                )
                await connections.broadcast_dashboard(
                    {
                        "type": "node.updated",
                        "node_id": node_id,
                        "online": True,
                        "message_type": message_type,
                    }
                )

            elif message_type == "environment.telemetry":
                environment = NodeEnvironmentRepository(db).upsert(
                    node_id=node_id,
                    sensor_driver=message.get("sensor"),
                    temperature_c=float(message["temperature_c"]),
                    humidity_percent=float(message["humidity_percent"]),
                    pressure_hpa=(
                        float(message["pressure_hpa"])
                        if message.get("pressure_hpa") is not None
                        else None
                    ),
                    dew_point_c=(
                        float(message["dew_point_c"])
                        if message.get("dew_point_c") is not None
                        else None
                    ),
                    captured_at=parse_iso_datetime(message.get("captured_at")),
                )
                await connections.broadcast_dashboard(
                    {
                        "type": "environment.updated",
                        "node_id": node_id,
                        "telemetry": environment_to_dict(environment),
                    }
                )

            elif message_type == "heater.state":
                heater_state = NodeHeaterStateRepository(db).update_actual(
                    node_id=node_id,
                    enabled=bool(message.get("enabled", False)),
                    driver=message.get("driver"),
                    gpio_pin=message.get("gpio_pin"),
                )
                await connections.broadcast_dashboard(
                    {
                        "type": "heater.updated",
                        "node_id": node_id,
                        "heater": heater_state_to_dict(heater_state),
                    }
                )

            elif message_type == "device.configured":
                heater_payload = message.get("heater") or {}
                heater_state = NodeHeaterStateRepository(db).update_actual(
                    node_id=node_id,
                    enabled=bool(heater_payload.get("enabled", False)),
                    driver=heater_payload.get("driver"),
                    gpio_pin=heater_payload.get("gpio_pin"),
                )
                await connections.broadcast_dashboard(
                    {
                        "type": "device.configured",
                        "node_id": node_id,
                        "devices": message.get("devices", {}),
                        "heater": heater_state_to_dict(heater_state),
                    }
                )

            await websocket.send_json(
                {
                    "type": "server.ack",
                    "received_type": message_type,
                }
            )

    except WebSocketDisconnect:
        connections.disconnect(node_id)
        repo.mark_offline(node_id)
        logger.warning("node.disconnected", node_id=node_id)
        await connections.broadcast_dashboard(
            {
                "type": "node.updated",
                "node_id": node_id,
                "online": False,
            }
        )

    finally:
        db.close()


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    if not await dashboard_socket_is_authorised(websocket):
        return

    await connections.connect_dashboard(websocket)

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        connections.disconnect_dashboard(websocket)


def _frontend_dist_file(relative_path: str) -> Path | None:
    """Resolve a request path to a file in the built frontend, or None.

    Anything that resolves outside the dist directory is rejected, so a crafted
    path cannot walk out of it.
    """
    if not relative_path or relative_path.endswith("/"):
        return None

    dist_dir = settings.frontend_dist_dir.resolve()

    try:
        candidate = (dist_dir / relative_path).resolve()

    except (OSError, ValueError):
        return None

    if not candidate.is_relative_to(dist_dir) or not candidate.is_file():
        return None

    return candidate


@app.get("/{frontend_path:path}", include_in_schema=False)
async def frontend_route(frontend_path: str):
    # Vite copies frontend/public/ to the dist root, so the logo, favicons and
    # friends sit beside index.html rather than under the mounted /assets.
    static_path = _frontend_dist_file(frontend_path)

    if static_path is not None:
        return FileResponse(static_path)

    first_segment = frontend_path.split("/", 1)[0]

    # Mirrors the client router. An allowlist rather than a catch-all so a typo'd
    # URL is still a 404 instead of a page that renders and then fails.
    # Mirrors the client router. An allowlist rather than a catch-all so a typo'd
    # URL is still a 404 instead of a page that renders and then fails.
    if first_segment not in {
        "login", "monitor", "captures", "products", "overlays", "settings", "nodes"
    }:
        raise HTTPException(status_code=404, detail="Not found")

    index_path = settings.frontend_dist_dir / "index.html"

    if index_path.exists():
        return FileResponse(index_path)

    return await frontend_app()


