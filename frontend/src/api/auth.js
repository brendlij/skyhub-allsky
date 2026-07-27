import { ref } from "vue";

/* The browser holds no credential of its own any more.
 *
 * The session is an HttpOnly cookie the server sets and this code cannot read -
 * which is the point: a cross-site scripting bug can call the API as the user,
 * but it cannot walk off with a token the way it could read one out of
 * localStorage. Every request below just says `credentials: "same-origin"` and
 * lets the browser attach the cookie.
 *
 * The one value script does read is the CSRF token, from a deliberately
 * non-HttpOnly cookie, and echoes back in a header. On its own it authenticates
 * nothing - the server only ever checks it alongside the session cookie.
 */

const CSRF_COOKIE = "skyhub_csrf";
const CSRF_HEADER = "X-CSRF-Token";

/** Reactive mirror of the server's answer to "who is this?". */
export const authState = ref({
  loading: true,
  setupRequired: false,
  setupOpen: false,
  authenticated: false,
  totpPending: false,
  totpEnrolled: false,
  username: null,
  apiKeyRequired: false,
  sessionIdleMinutes: 30
});

export function readCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)skyhub_csrf=([^;]*)/);

  return match ? decodeURIComponent(match[1]) : "";
}

export class AuthError extends Error {
  constructor(message, status, retryAfter = 0) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

/* FastAPI reports failures as {"detail": …}. Showing raw JSON in a form makes a
 * perfectly clear message look like a crash. */
async function errorFrom(response) {
  const retryAfter = Number(response.headers.get("Retry-After")) || 0;

  let detail = response.statusText;

  try {
    const body = await response.json();

    if (typeof body?.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body?.detail)) {
      // Pydantic validation errors arrive as a list of {loc, msg, …}.
      detail = body.detail.map((item) => item.msg).filter(Boolean).join(", ");
    }
  } catch {
    // Not JSON - keep the status text.
  }

  return new AuthError(detail, response.status, retryAfter);
}

export async function authRequest(path, { method = "GET", body = null } = {}) {
  const headers = {};

  if (body !== null) headers["Content-Type"] = "application/json";

  // Only the unsafe methods are checked server side, but sending it always is
  // harmless and means a method change here cannot silently drop the header.
  const csrf = readCsrfToken();
  if (csrf) headers[CSRF_HEADER] = csrf;

  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === null ? undefined : JSON.stringify(body)
  });

  if (!response.ok) throw await errorFrom(response);

  return response.status === 204 ? null : response.json();
}

/** Ask the server what state we are in. Safe to call unauthenticated. */
export async function refreshAuthState() {
  try {
    const status = await authRequest("/api/auth/status");

    authState.value = {
      loading: false,
      setupRequired: Boolean(status.setup_required),
      setupOpen: Boolean(status.setup_open),
      authenticated: Boolean(status.authenticated),
      totpPending: Boolean(status.totp_pending),
      totpEnrolled: Boolean(status.totp_enrolled),
      username: status.username || null,
      apiKeyRequired: Boolean(status.api_key_required),
      sessionIdleMinutes: Number(status.session_idle_minutes) || 30
    };
  } catch {
    // The server is unreachable rather than saying no. Treat it as signed out
    // so the UI lands on the login page instead of an empty dashboard.
    authState.value = { ...authState.value, loading: false, authenticated: false };
  }

  return authState.value;
}

export function markSignedOut() {
  authState.value = {
    ...authState.value,
    authenticated: false,
    totpPending: false,
    username: null
  };
}

// ---------- first run ----------

export const beginSetup = (setupToken, username, password) => authRequest("/api/auth/setup", {
  method: "POST",
  body: { setup_token: setupToken, username, password }
});

export const confirmSetup = (code, rememberDevice) => authRequest("/api/auth/setup/confirm", {
  method: "POST",
  body: { code, remember_device: Boolean(rememberDevice) }
});

// ---------- login ----------

export const login = (username, password) => authRequest("/api/auth/login", {
  method: "POST",
  body: { username, password }
});

export const submitTotp = (code, rememberDevice) => authRequest("/api/auth/totp", {
  method: "POST",
  body: { code, remember_device: Boolean(rememberDevice) }
});

export const logout = () => authRequest("/api/auth/logout", { method: "POST" });

// ---------- account management ----------

export const listSessions = () => authRequest("/api/auth/sessions");

export const revokeOtherSessions = () => authRequest("/api/auth/sessions/revoke-others", {
  method: "POST"
});

export const changePassword = (currentPassword, newPassword) => authRequest("/api/auth/password", {
  method: "POST",
  body: { current_password: currentPassword, new_password: newPassword }
});

export const beginTotpReset = (currentPassword) => authRequest("/api/auth/totp/reset", {
  method: "POST",
  body: { current_password: currentPassword }
});

export const confirmTotpReset = (code) => authRequest("/api/auth/totp/reset/confirm", {
  method: "POST",
  body: { code }
});

export const listTrustedDevices = () => authRequest("/api/auth/devices");

export const forgetTrustedDevices = () => authRequest("/api/auth/devices/forget", {
  method: "POST"
});
