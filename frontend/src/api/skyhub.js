import { ref } from "vue";

// The server only demands this when SKYHUB_SERVER_API_KEY is set; an install
// without one never sees a prompt. Kept in localStorage because there is no
// login: the key is the whole credential, and it is a LAN tool.
const API_KEY_STORAGE = "skyhub.api_key";

export const apiKey = ref(localStorage.getItem(API_KEY_STORAGE) || "");
export const apiKeyRequired = ref(false);

export function setApiKey(value) {
  const key = String(value || "").trim();

  apiKey.value = key;

  if (key) {
    localStorage.setItem(API_KEY_STORAGE, key);
  } else {
    localStorage.removeItem(API_KEY_STORAGE);
  }
}

function authHeaders() {
  return apiKey.value ? { "X-API-Key": apiKey.value } : {};
}

/** Append the key to a URL the browser fetches without us: <img>, WebSocket. */
export function withApiKey(url) {
  if (!apiKey.value) return url;

  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}api_key=${encodeURIComponent(apiKey.value)}`;
}

export async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) }
  });

  if (response.status === 401) {
    // Surfaces the key prompt rather than a wall of failed-request toasts.
    apiKeyRequired.value = true;
    throw new Error("This server needs an API key");
  }

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }

  apiKeyRequired.value = false;
  return response.json();
}

export function captureUrl(capture, options = {}) {
  const url = `/api/captures/${capture.node_id}/${capture.archive_date}/${capture.period}/${capture.filename}`;
  const params = new URLSearchParams();

  if (options.raw) params.set("raw", "true");
  if (options.thumb) params.set("thumb", "true");
  // An <img> cannot carry a header, so the key has to ride in the query string.
  if (apiKey.value) params.set("api_key", apiKey.value);

  const query = params.toString();
  return query ? `${url}?${query}` : url;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "-";

  if (bytes < 1024) return `${bytes} B`;

  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;

  for (const unit of units) {
    if (value < 1024 || unit === "GB") {
      return `${value.toFixed(value >= 10 ? 0 : 1)} ${unit}`;
    }

    value /= 1024;
  }

  return `${bytes} B`;
}

export function formatDateTime(value) {
  if (!value) return "-";

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium"
  }).format(new Date(value));
}

export function formatCaptureName(capture) {
  if (!capture) return "";

  const date = capture.archive_date || "unknown date";
  const period = capture.period || "capture";
  const node = capture.node_id || "node";
  const time = capture.modified_at
    ? new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      }).format(new Date(capture.modified_at))
    : "";

  return [date, time, period, node].filter(Boolean).join(" - ");
}

export function preloadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = resolve;
    image.onerror = reject;
    image.src = url;
  });
}
