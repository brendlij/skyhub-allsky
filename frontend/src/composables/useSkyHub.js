import { computed, ref } from "vue";
import { authState } from "../api/auth";
import { captureUrl, preloadImage, requestJson, withApiKey } from "../api/skyhub";
import { confirmAction } from "./useConfirm";
import { useToasts } from "./useToasts";

const { notify, notifyError, pushToast } = useToasts();

const fields = [
  "interval_seconds",
  "day_interval_seconds",
  "night_interval_seconds",
  "full_resolution",
  "width",
  "height",
  "format",
  "day_auto_exposure",
  "day_exposure_ms",
  "day_max_exposure_ms",
  "day_auto_gain",
  "day_gain",
  "day_max_gain",
  "night_auto_exposure",
  "night_exposure_ms",
  "night_max_exposure_ms",
  "night_auto_gain",
  "night_gain",
  "night_max_gain",
  "day_auto_white_balance",
  "day_wb_red",
  "day_wb_blue",
  "day_saturation",
  "day_hue",
  "night_auto_white_balance",
  "night_wb_red",
  "night_wb_blue",
  "night_saturation",
  "night_hue"
];

const nodes = ref([]);
const selectedNodeId = ref(null);
const settings = ref(null);
const latest = ref(null);
const latestImageUrl = ref(null);
const captures = ref([]);
const captureDates = ref([]);
const captureTotal = ref(0);
const overlaySettings = ref(null);
const deviceSettings = ref(null);
const environmentTelemetry = ref(null);
const heaterState = ref(null);
const storageStats = ref(null);
const storageSettings = ref(null);
const loading = ref(false);
// "live" | "connecting" | "offline" - previously the socket could drop and
// reconnect forever with nothing in the UI to say the data had gone stale.
const connectionState = ref("connecting");
const lastUpdatedAt = ref(null);
let initialized = false;
let dashboardSocket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let wasDisconnected = false;
let captureScope = { archiveDate: null, period: null };

const selectedNode = computed(() =>
  nodes.value.find((node) => node.node_id === selectedNodeId.value)
);

function setMessage(text, tone = "success") {
  if (!text) return;
  pushToast(text, { tone });
}

function settingsFromApi(data) {
  return {
    interval_seconds: data.interval_seconds,
    day_interval_seconds: data.day_interval_seconds,
    night_interval_seconds: data.night_interval_seconds,
    full_resolution: data.full_resolution,
    width: data.width,
    height: data.height,
    format: data.format,
    day_auto_exposure: data.day.auto_exposure,
    day_exposure_ms: data.day.exposure_ms,
    day_max_exposure_ms: data.day.max_exposure_ms,
    day_auto_gain: data.day.auto_gain,
    day_gain: data.day.gain,
    day_max_gain: data.day.max_gain,
    night_auto_exposure: data.night.auto_exposure,
    night_exposure_ms: data.night.exposure_ms,
    night_max_exposure_ms: data.night.max_exposure_ms,
    night_auto_gain: data.night.auto_gain,
    night_gain: data.night.gain,
    night_max_gain: data.night.max_gain,
    day_auto_white_balance: data.day.auto_white_balance,
    day_wb_red: data.day.wb_red,
    day_wb_blue: data.day.wb_blue,
    day_saturation: data.day.saturation,
    day_hue: data.day.hue,
    night_auto_white_balance: data.night.auto_white_balance,
    night_wb_red: data.night.wb_red,
    night_wb_blue: data.night.wb_blue,
    night_saturation: data.night.saturation,
    night_hue: data.night.hue,
    capture_enabled: data.capture_enabled,
    current_sequence_id: data.current_sequence_id
  };
}

function deviceSettingsFromApi(data) {
  return {
    ...data,
    devices: {
      environment: {
        driver: "bme280",
        interval_seconds: 30,
        bme280_i2c_bus: 1,
        bme280_i2c_address: "0x77",
        ...(data.devices?.environment || {})
      },
      heater: {
        driver: "gpiozero",
        gpio_pin: 23,
        active_high: true,
        mode: "manual",
        pwm: {
          enabled: false,
          duty_cycle: 1.0,
          ...(data.devices?.heater?.pwm || {})
        },
        ...(data.devices?.heater || {})
      }
    }
  };
}

function payloadValue(value) {
  return value === "" ? null : value;
}

async function loadNodes() {
  const data = await requestJson("/api/nodes");
  nodes.value = data.nodes;

  const onlineNode = nodes.value.find((node) => node.online);
  const current = nodes.value.find((node) => node.node_id === selectedNodeId.value);

  if ((!selectedNodeId.value || !current || (!current.online && onlineNode)) && (onlineNode || nodes.value.length)) {
    selectedNodeId.value = (onlineNode || nodes.value[0]).node_id;
  }
}

async function loadSettings() {
  if (!selectedNodeId.value) {
    settings.value = null;
    return;
  }

  settings.value = settingsFromApi(
    await requestJson(`/api/nodes/${selectedNodeId.value}/settings`)
  );
}

async function loadOverlays() {
  if (!selectedNodeId.value) {
    overlaySettings.value = null;
    return;
  }

  overlaySettings.value = await requestJson(`/api/nodes/${selectedNodeId.value}/overlays`);
}

async function loadDeviceSettings() {
  if (!selectedNodeId.value) {
    deviceSettings.value = null;
    return;
  }

  deviceSettings.value = deviceSettingsFromApi(
    await requestJson(`/api/nodes/${selectedNodeId.value}/devices`)
  );
}

async function loadEnvironmentTelemetry() {
  if (!selectedNodeId.value) {
    environmentTelemetry.value = null;
    return;
  }

  try {
    environmentTelemetry.value = await requestJson(`/api/nodes/${selectedNodeId.value}/environment`);
  } catch {
    environmentTelemetry.value = null;
  }
}

async function loadHeaterState() {
  if (!selectedNodeId.value) {
    heaterState.value = null;
    return;
  }

  heaterState.value = await requestJson(`/api/nodes/${selectedNodeId.value}/heater`);
}

async function loadStorageStats() {
  storageStats.value = await requestJson("/api/storage");
}

async function loadStorageSettings() {
  storageSettings.value = await requestJson("/api/storage/settings");
}

async function loadLatest() {
  if (!selectedNodeId.value) {
    latest.value = null;
    latestImageUrl.value = null;
    return;
  }

  try {
    const nextLatest = await requestJson(`/api/captures/latest?node_id=${encodeURIComponent(selectedNodeId.value)}`);
    const currentKey = latest.value
      ? `${latest.value.node_id}/${latest.value.archive_date}/${latest.value.period}/${latest.value.filename}`
      : null;
    const nextKey = `${nextLatest.node_id}/${nextLatest.archive_date}/${nextLatest.period}/${nextLatest.filename}`;

    if (currentKey !== nextKey) {
      const nextUrl = captureUrl(nextLatest);
      await preloadImage(nextUrl);
      latest.value = nextLatest;
      latestImageUrl.value = nextUrl;
    }
  } catch {
    latest.value = null;
    latestImageUrl.value = null;
  }
}

async function loadCaptureDates() {
  if (!selectedNodeId.value) {
    captureDates.value = [];
    return;
  }

  const data = await requestJson(
    `/api/captures/dates?node_id=${encodeURIComponent(selectedNodeId.value)}`
  );
  captureDates.value = data.dates;
}

async function loadCaptures(scope = {}) {
  if (!selectedNodeId.value) return;

  const archiveDate = scope.archiveDate ?? captureScope.archiveDate;
  const period = scope.period ?? captureScope.period;
  captureScope = { archiveDate, period };

  const params = new URLSearchParams({ node_id: selectedNodeId.value, limit: "0" });

  // Scoping the request to one date and period is what stops older nights from
  // being cut off by a limit shared across the whole archive.
  if (archiveDate) params.set("archive_date", archiveDate);
  if (period) params.set("period", period);

  const data = await requestJson(`/api/captures?${params.toString()}`);
  captures.value = data.captures;
  captureTotal.value = data.total;
}

/* One unreachable endpoint used to reject the whole Promise.all and leave the
 * dashboard half-loaded with a single opaque message. Settle everything, then
 * report the failures together. */
async function loadAll(tasks) {
  const results = await Promise.allSettled(tasks.map(([, run]) => run()));
  const failed = results
    .map((result, index) => (result.status === "rejected" ? tasks[index][0] : null))
    .filter(Boolean);

  if (failed.length) {
    pushToast(
      `Could not load: ${failed.join(", ")}`,
      { tone: "warning", title: "Partial refresh" }
    );
  }

  lastUpdatedAt.value = new Date().toISOString();
  return failed;
}

function nodeScopedLoaders() {
  return [
    ["settings", loadSettings],
    ["overlays", loadOverlays],
    ["devices", loadDeviceSettings],
    ["environment", loadEnvironmentTelemetry],
    ["heater", loadHeaterState],
    ["latest capture", loadLatest],
    ["capture dates", loadCaptureDates],
    ["captures", loadCaptures]
  ];
}

async function refreshDashboard() {
  loading.value = true;

  try {
    try {
      await loadNodes();
    } catch (error) {
      notifyError(error, { title: "Could not reach the server" });
      return;
    }

    await loadAll([
      ...nodeScopedLoaders(),
      ["storage usage", loadStorageStats],
      ["storage policy", loadStorageSettings]
    ]);
  } finally {
    loading.value = false;
  }
}

async function selectNode(nodeId) {
  if (!nodeId || nodeId === selectedNodeId.value) return;

  selectedNodeId.value = nodeId;
  captureScope = { archiveDate: null, period: null };
  await loadAll(nodeScopedLoaders());
}

async function deleteNode(nodeId) {
  const confirmed = await confirmAction({
    title: `Delete ${nodeId}?`,
    message:
      "The node record and its settings, overlays and telemetry are removed. Captures already on disk are kept.",
    confirmLabel: "Delete node"
  });

  if (!confirmed) return;

  try {
    await requestJson(`/api/nodes/${nodeId}`, { method: "DELETE" });
  } catch (error) {
    notifyError(error, { title: "Delete failed" });
    return;
  }

  if (selectedNodeId.value === nodeId) {
    selectedNodeId.value = null;
  }

  notify(`Deleted ${nodeId}`);
  await refreshDashboard();
}

/* Views call these straight from @click. Without a catch here a failed request
 * became an unhandled rejection with nothing shown to the user. `busy` lets the
 * button that triggered the action disable itself while it runs. */
const busy = ref({});

async function runAction(key, label, run) {
  busy.value = { ...busy.value, [key]: true };

  try {
    return await run();
  } catch (error) {
    notifyError(error, { title: label });
    return null;
  } finally {
    const next = { ...busy.value };
    delete next[key];
    busy.value = next;
  }
}

function putJson(url, body) {
  return requestJson(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
}

function saveSettings() {
  if (!selectedNodeId.value || !settings.value) return Promise.resolve();

  return runAction("settings", "Could not save camera settings", async () => {
    const body = {};

    for (const field of fields) {
      body[field] = payloadValue(settings.value[field]);
    }

    if (!body.format) body.format = "jpg";

    const result = await putJson(`/api/nodes/${selectedNodeId.value}/settings`, body);
    settings.value = settingsFromApi(result.settings);

    if (!result.node_notified) {
      notify("Settings saved (node offline)");
    } else if (result.settings?.capture_enabled) {
      // The node picks these up before its next frame, so no restart is needed -
      // but the capture already in flight still finishes on the old settings.
      notify("Settings saved - the node applies them on its next capture");
    } else {
      notify("Settings saved and sent to node");
    }
  });
}

function saveOverlays() {
  if (!selectedNodeId.value || !overlaySettings.value) return Promise.resolve();

  return runAction("overlays", "Could not save overlays", async () => {
    const result = await putJson(`/api/nodes/${selectedNodeId.value}/overlays`, {
      enabled: overlaySettings.value.enabled,
      entities: overlaySettings.value.entities
    });

    overlaySettings.value = result;

    if (result.warnings?.length) {
      pushToast(
        `Unknown variables saved: ${result.warnings.map((w) => w.token).join(", ")}`,
        { tone: "warning", title: "These will render as empty text" }
      );
    } else {
      notify("Overlays saved");
    }
  });
}

function saveDeviceSettings() {
  if (!selectedNodeId.value || !deviceSettings.value) return Promise.resolve();

  return runAction("devices", "Could not save device settings", async () => {
    const result = await putJson(`/api/nodes/${selectedNodeId.value}/devices`, {
      devices: deviceSettings.value.devices
    });

    deviceSettings.value = deviceSettingsFromApi(result.device_settings);
    notify(result.node_notified ? "Devices saved and sent to node" : "Devices saved (node offline)");
  });
}

function saveStorageSettings() {
  if (!storageSettings.value) return Promise.resolve();

  return runAction("storage", "Could not save storage policy", async () => {
    const result = await putJson("/api/storage/settings", {
      day_capture_enabled: storageSettings.value.day_capture_enabled,
      night_capture_enabled: storageSettings.value.night_capture_enabled,
      retention_days: storageSettings.value.retention_days || null,
      max_storage_gb: storageSettings.value.max_storage_gb || null
    });

    storageSettings.value = result.storage_settings;
    await loadStorageStats().catch(() => {});
    notify("Storage policy saved");
  });
}

function startCapture() {
  if (!selectedNodeId.value) return Promise.resolve();

  return runAction("capture", "Could not start capture", async () => {
    const result = await requestJson(`/api/nodes/${selectedNodeId.value}/sequence/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });

    if (result.status === "queued") {
      pushToast("Node is offline - capture queued until it reconnects", { tone: "warning" });
    } else {
      notify("Capture started");
    }

    await loadSettings().catch(() => {});
  });
}

function stopCapture() {
  if (!selectedNodeId.value) return Promise.resolve();

  return runAction("capture", "Could not stop capture", async () => {
    await requestJson(`/api/nodes/${selectedNodeId.value}/sequence/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });

    notify("Capture stopped");
    await loadSettings().catch(() => {});
  });
}

function setHeaterEnabled(enabled) {
  if (!selectedNodeId.value) return Promise.resolve();

  return runAction("heater", "Could not switch the heater", async () => {
    const result = await putJson(`/api/nodes/${selectedNodeId.value}/heater`, { enabled });
    heaterState.value = result.heater;
    notify(result.node_notified
      ? `Heater ${enabled ? "on" : "off"}`
      : `Heater ${enabled ? "on" : "off"} saved (node offline)`);
  });
}

function dashboardWebSocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";

  // A browser WebSocket cannot send headers, which used to mean the credential
  // rode in the query string. The session cookie is attached to the handshake
  // by the browser itself, so the URL now carries nothing secret.
  return withApiKey(`${protocol}//${window.location.host}/ws/dashboard`);
}

function captureMatchesScope(capture) {
  if (captureScope.archiveDate && capture.archive_date !== captureScope.archiveDate) return false;
  if (captureScope.period && capture.period !== captureScope.period) return false;

  return true;
}

async function applyCaptureUploaded(event) {
  if (!event.capture || event.node_id !== selectedNodeId.value) return;

  const exists = captures.value.some((capture) => capture.path === event.capture.path);

  if (!exists && captureMatchesScope(event.capture)) {
    captures.value = [event.capture, ...captures.value];
    captureTotal.value += 1;
  }

  if (!exists) {
    loadCaptureDates().catch(() => {});
  }

  const nextUrl = captureUrl(event.capture);

  try {
    await preloadImage(nextUrl);
    latest.value = event.capture;
    latestImageUrl.value = nextUrl;
  } catch {
    await loadLatest();
  }
}

function handleDashboardEvent(event) {
  if (event.type === "capture.uploaded") {
    applyCaptureUploaded(event).catch(() => {});
    loadStorageStats().catch(() => {});
    return;
  }

  if (event.type === "storage.settings.updated") {
    storageSettings.value = event.storage_settings;
    loadStorageStats().catch(() => {});
    return;
  }

  if (event.type === "settings.updated" && event.node_id === selectedNodeId.value) {
    settings.value = settingsFromApi(event.settings);
    return;
  }

  if (event.type === "overlay.updated" && event.node_id === selectedNodeId.value) {
    overlaySettings.value = event.overlays;
    return;
  }

  if (event.type === "device.settings.updated" && event.node_id === selectedNodeId.value) {
    deviceSettings.value = deviceSettingsFromApi(event.device_settings);
    return;
  }

  if (event.type === "device.configured" && event.node_id === selectedNodeId.value) {
    if (event.heater) heaterState.value = event.heater;
    return;
  }

  if (event.type === "capture.state.updated" && event.node_id === selectedNodeId.value) {
    loadSettings().catch(() => {});
    return;
  }

  if (event.type === "environment.updated" && event.node_id === selectedNodeId.value) {
    environmentTelemetry.value = event.telemetry;
    return;
  }

  if (event.type === "heater.updated" && event.node_id === selectedNodeId.value) {
    heaterState.value = event.heater;
    return;
  }

  if (
    event.type === "processing.products"
    || event.type === "processing.session"
    || event.type === "processing.progress"
  ) {
    // Fanned out rather than handled here: the products view owns that state, and
    // this composable should not grow a dependency on a screen most sessions
    // never open.
    for (const listener of processingListeners) listener(event);

    return;
  }

  if (event.type === "node.updated" || event.type === "node.deleted") {
    loadNodes().catch(() => {});
  }
}

/* Subscription for the processing views. A Set so a component that mounts twice
 * during a hot reload does not end up handling every event twice. */
const processingListeners = new Set();

export function onProcessingEvent(listener) {
  processingListeners.add(listener);

  return () => processingListeners.delete(listener);
}

function connectDashboardSocket() {
  if (dashboardSocket && dashboardSocket.readyState < WebSocket.CLOSING) return;

  connectionState.value = "connecting";
  dashboardSocket = new WebSocket(dashboardWebSocketUrl());

  dashboardSocket.onopen = () => {
    const reconnected = wasDisconnected;
    wasDisconnected = false;
    reconnectDelay = 1000;
    connectionState.value = "live";

    // Events that arrived while the socket was down were missed, so resync
    // rather than carrying on with stale data.
    if (reconnected) {
      refreshDashboard().catch(() => {});
    }
  };

  dashboardSocket.onmessage = (socketEvent) => {
    lastUpdatedAt.value = new Date().toISOString();

    try {
      handleDashboardEvent(JSON.parse(socketEvent.data));
    } catch {
      // Ignore malformed dashboard events.
    }
  };

  dashboardSocket.onclose = () => {
    dashboardSocket = null;
    wasDisconnected = true;
    connectionState.value = "offline";

    if (reconnectTimer === null) {
      // Back off instead of hammering every 2s while the server is down.
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectDashboardSocket();
      }, reconnectDelay);

      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }
  };
}

function ensureRealtimeRefresh() {
  if (initialized) return;

  /* Nothing starts before there is a session. The login page mounts the same
   * shell as everything else, and without this it would open a WebSocket the
   * server closes and a round of requests the server 401s - a reconnect loop
   * behind a login form. `initialized` stays false, so the first view to render
   * after a successful login starts it all properly. */
  if (!authState.value.authenticated) return;

  initialized = true;

  refreshDashboard().catch((error) => notifyError(error));
  connectDashboardSocket();
}

/** Tear the live connection down on sign-out, so it cannot reconnect as nobody. */
export function stopRealtime() {
  initialized = false;
  connectionState.value = "offline";

  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (dashboardSocket) {
    // Drop the handler first: onclose would otherwise schedule a reconnect.
    dashboardSocket.onclose = null;
    dashboardSocket.close();
    dashboardSocket = null;
  }
}

export function useSkyHub() {
  ensureRealtimeRefresh();

  return {
    nodes,
    selectedNodeId,
    selectedNode,
    settings,
    latest,
    latestImageUrl,
    captures,
    captureDates,
    captureTotal,
    overlaySettings,
    deviceSettings,
    environmentTelemetry,
    heaterState,
    storageStats,
    storageSettings,
    loading,
    busy,
    connectionState,
    lastUpdatedAt,
    captureUrl,
    deleteNode,
    loadCaptures,
    loadCaptureDates,
    loadDeviceSettings,
    loadEnvironmentTelemetry,
    loadHeaterState,
    loadOverlays,
    loadStorageSettings,
    loadStorageStats,
    refreshDashboard,
    saveSettings,
    saveDeviceSettings,
    saveOverlays,
    saveStorageSettings,
    selectNode,
    setHeaterEnabled,
    setMessage,
    startCapture,
    stopCapture
  };
}
