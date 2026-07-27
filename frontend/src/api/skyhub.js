import { markSignedOut, readCsrfToken } from "./auth";

/* The browser carries no API key any more.
 *
 * Humans authenticate with a session cookie the server sets on login: HttpOnly,
 * SameSite=Strict, and revocable. The cookie is attached by the browser to every
 * request on this origin - including <img> loads and the WebSocket handshake -
 * which is why nothing here has to sign a URL. The shared API key still exists,
 * but it belongs to camera nodes and scripts, and is never handed to a browser.
 *
 * See api/auth.js for the session itself.
 */

function authHeaders() {
  const csrf = readCsrfToken();

  return csrf ? { "X-CSRF-Token": csrf } : {};
}

/** Kept for call sites that build a URL the browser fetches on its own.
 *
 * Now a no-op: the session cookie travels with same-origin requests by itself,
 * so an image or socket URL needs nothing appended. Left in place so those call
 * sites keep documenting *why* they were special. */
export function withApiKey(url) {
  return url;
}

/* FastAPI reports failures as {"detail": "…"}. Showing the raw JSON in a toast
 * makes a perfectly clear message look like a crash. */
function errorMessage(text) {
  if (!text) return "";

  try {
    const parsed = JSON.parse(text);
    const detail = parsed?.detail;

    if (typeof detail === "string") return detail;
    // Validation errors come back as a list of {loc, msg, …}.
    if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join(", ");

  } catch {
    // Not JSON - the body is the message.
  }

  return text;
}

export async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    headers: { ...authHeaders(), ...(options.headers || {}) }
  });

  if (response.status === 401) {
    /* The session expired, was revoked from another browser, or never existed.
     * Flip the shared state and let the router guard move to the login page -
     * far better than a wall of failed-request toasts. */
    markSignedOut();
    throw new Error("Your session has ended. Sign in again.");
  }

  if (!response.ok) {
    const text = await response.text();
    throw new Error(errorMessage(text) || response.statusText);
  }

  // A 204 (delete) has no body, and response.json() would throw on it.
  if (response.status === 204) return null;

  return response.json();
}

export function captureUrl(capture, options = {}) {
  const url = `/api/captures/${capture.node_id}/${capture.archive_date}/${capture.period}/${capture.filename}`;
  const params = new URLSearchParams();

  if (options.raw) params.set("raw", "true");
  if (options.thumb) params.set("thumb", "true");

  // No credential in the query string: the <img> that loads this carries the
  // session cookie on its own, so the URL is safe to log, cache and share.
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
