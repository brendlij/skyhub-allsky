<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  authState,
  beginTotpReset,
  changePassword,
  confirmTotpReset,
  forgetTrustedDevices,
  listSessions,
  listTrustedDevices,
  logout,
  markSignedOut,
  refreshAuthState,
  revokeOtherSessions
} from "../api/auth";
import { confirmAction } from "../composables/useConfirm";
import { stopRealtime } from "../composables/useSkyHub";
import { useToasts } from "../composables/useToasts";

const router = useRouter();
const { notify, notifyError } = useToasts();

const sessions = ref([]);
const devices = ref([]);
const busy = ref("");

const passwordForm = ref({ open: false, current: "", next: "", repeat: "" });
const totpForm = ref({ open: false, current: "", code: "", enrolment: null, secretVisible: false });

const username = computed(() => authState.value.username || "admin");

const passwordReady = computed(() => {
  const form = passwordForm.value;

  return Boolean(form.current && form.next) && form.next === form.repeat && form.next.length >= 12;
});

const passwordMismatch = computed(() => {
  const form = passwordForm.value;

  return form.repeat.length > 0 && form.next !== form.repeat;
});

onMounted(refresh);

async function refresh() {
  try {
    const [sessionResult, deviceResult] = await Promise.all([listSessions(), listTrustedDevices()]);

    sessions.value = sessionResult.sessions || [];
    devices.value = deviceResult.devices || [];
  } catch (error) {
    notifyError(error);
  }
}

/* A user agent string is unreadable and the interesting part is the platform.
 * This is a hint for recognising your own browser, not forensics. */
function describeAgent(agent) {
  if (!agent) return "Unknown browser";

  const browser = /Firefox\/[\d.]+/.test(agent) ? "Firefox"
    : /Edg\//.test(agent) ? "Edge"
      : /OPR\//.test(agent) ? "Opera"
        : /Chrome\//.test(agent) ? "Chrome"
          : /Safari\//.test(agent) ? "Safari"
            : "Browser";

  const platform = /Windows/.test(agent) ? "Windows"
    : /Android/.test(agent) ? "Android"
      : /iPhone|iPad/.test(agent) ? "iOS"
        : /Mac OS X/.test(agent) ? "macOS"
          : /Linux/.test(agent) ? "Linux"
            : "";

  return platform ? `${browser} on ${platform}` : browser;
}

function formatMoment(value) {
  if (!value) return "—";

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) return "—";

  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

async function run(key, action) {
  busy.value = key;

  try {
    await action();
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = "";
  }
}

async function submitPassword() {
  if (!passwordReady.value) return;

  await run("password", async () => {
    const result = await changePassword(passwordForm.value.current, passwordForm.value.next);

    passwordForm.value = { open: false, current: "", next: "", repeat: "" };
    notify(
      result.other_sessions_revoked
        ? `Password changed. ${result.other_sessions_revoked} other session(s) signed out.`
        : "Password changed."
    );
    await refresh();
  });
}

async function startTotpReset() {
  if (!totpForm.value.current) return;

  await run("totp", async () => {
    totpForm.value.enrolment = await beginTotpReset(totpForm.value.current);
    totpForm.value.current = "";
    totpForm.value.code = "";
  });
}

async function finishTotpReset() {
  if (totpForm.value.code.replace(/\D/g, "").length !== 6) return;

  await run("totp", async () => {
    const result = await confirmTotpReset(totpForm.value.code);

    totpForm.value = { open: false, current: "", code: "", enrolment: null, secretVisible: false };
    notify(
      `Authenticator replaced. ${result.other_sessions_revoked} other session(s) and `
      + `${result.trusted_devices_forgotten} remembered browser(s) signed out.`
    );
    await refresh();
    await refreshAuthState();
  });
}

async function revokeOthers() {
  const confirmed = await confirmAction({
    title: "Sign out every other browser?",
    message: "Any other device signed in to SkyHub has to log in again. This browser stays signed in.",
    confirmLabel: "Sign the others out",
    tone: "danger"
  });

  if (!confirmed) return;

  await run("sessions", async () => {
    const result = await revokeOtherSessions();

    notify(result.revoked ? `${result.revoked} session(s) signed out.` : "No other sessions.");
    await refresh();
  });
}

async function forgetDevices() {
  const confirmed = await confirmAction({
    title: "Forget every remembered browser?",
    message: "Each will ask for a two-factor code at the next sign-in, including this one.",
    confirmLabel: "Forget them",
    tone: "danger"
  });

  if (!confirmed) return;

  await run("devices", async () => {
    const result = await forgetTrustedDevices();

    notify(`${result.trusted_devices_forgotten} remembered browser(s) forgotten.`);
    await refresh();
  });
}

async function signOut() {
  await run("logout", async () => {
    await logout();
    markSignedOut();
    stopRealtime();
    router.replace("/login");
  });
}
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <h2>
        Security
        <span class="badge success">2FA on</span>
      </h2>
    </div>

    <div class="panel-body">
      <dl class="data-list">
        <div class="data-row">
          <dt>Signed in as</dt>
          <dd class="data-value">{{ username }}</dd>
        </div>
        <div class="data-row">
          <dt>Password</dt>
          <dd class="data-value">Argon2id, never stored in plain text</dd>
        </div>
        <div class="data-row">
          <dt>Idle timeout</dt>
          <dd class="data-value">{{ authState.sessionIdleMinutes }} minutes</dd>
        </div>
      </dl>

      <!-- change password -->
      <div class="section">
        <div class="section-title">Password</div>

        <form v-if="passwordForm.open" class="security-form" @submit.prevent="submitPassword">
          <label class="field">
            <span>Current password</span>
            <input v-model="passwordForm.current" type="password" autocomplete="current-password" />
          </label>
          <label class="field">
            <span>New password <em class="field-unit">at least 12 characters</em></span>
            <input v-model="passwordForm.next" type="password" autocomplete="new-password" />
          </label>
          <label class="field">
            <span>Repeat new password</span>
            <input v-model="passwordForm.repeat" type="password" autocomplete="new-password" />
          </label>
          <p v-if="passwordMismatch" class="callout danger">The passwords do not match.</p>
          <p class="field-hint">
            Changing the password signs out every other browser — which is the point, if
            you are changing it because something leaked.
          </p>
          <div class="security-actions">
            <button type="submit" class="primary" :disabled="!passwordReady || busy === 'password'">
              {{ busy === "password" ? "Saving…" : "Change password" }}
            </button>
            <button type="button" class="ghost" @click="passwordForm = { open: false, current: '', next: '', repeat: '' }">
              Cancel
            </button>
          </div>
        </form>

        <button v-else type="button" class="sm" @click="passwordForm.open = true">
          Change password
        </button>
      </div>

      <!-- reset TOTP -->
      <div class="section">
        <div class="section-title">Authenticator app</div>

        <template v-if="totpForm.open">
          <form v-if="!totpForm.enrolment" class="security-form" @submit.prevent="startTotpReset">
            <p class="field-hint">
              Confirm your password to generate a new secret. The current authenticator
              keeps working until you enter a code from the new one.
            </p>
            <label class="field">
              <span>Current password</span>
              <input v-model="totpForm.current" type="password" autocomplete="current-password" />
            </label>
            <div class="security-actions">
              <button type="submit" class="primary" :disabled="!totpForm.current || busy === 'totp'">
                {{ busy === "totp" ? "Working…" : "Generate new secret" }}
              </button>
              <button type="button" class="ghost" @click="totpForm.open = false">Cancel</button>
            </div>
          </form>

          <form v-else class="security-form" @submit.prevent="finishTotpReset">
            <div class="security-qr">
              <!-- Server-rendered QR path data; no user input reaches the markup. -->
              <div class="security-qr-frame" v-html="totpForm.enrolment.qr_svg" />
              <button type="button" class="ghost sm" @click="totpForm.secretVisible = !totpForm.secretVisible">
                {{ totpForm.secretVisible ? "Hide key" : "Can't scan? Show the key" }}
              </button>
              <code v-if="totpForm.secretVisible" class="security-secret">
                {{ totpForm.enrolment.secret }}
              </code>
            </div>

            <label class="field">
              <span>Code from the new authenticator</span>
              <input
                v-model="totpForm.code"
                type="text"
                inputmode="numeric"
                maxlength="7"
                autocomplete="one-time-code"
                class="security-code"
                placeholder="000000"
              />
            </label>

            <p class="callout warning">
              Confirming retires the old secret, forgets every remembered browser and
              signs out every other session.
            </p>

            <div class="security-actions">
              <button type="submit" class="primary" :disabled="busy === 'totp'">
                {{ busy === "totp" ? "Working…" : "Confirm new authenticator" }}
              </button>
              <button
                type="button"
                class="ghost"
                @click="totpForm = { open: false, current: '', code: '', enrolment: null, secretVisible: false }"
              >
                Cancel
              </button>
            </div>
          </form>
        </template>

        <button v-else type="button" class="sm" @click="totpForm.open = true">
          Replace authenticator
        </button>
      </div>

      <!-- sessions -->
      <div class="section">
        <div class="row-between">
          <div class="section-title">Active sessions</div>
          <button type="button" class="ghost sm" @click="refresh">Refresh</button>
        </div>

        <ul class="session-list">
          <li v-for="session in sessions" :key="session.id" class="session-item">
            <div class="session-main">
              <strong>
                {{ describeAgent(session.user_agent) }}
                <span v-if="session.current" class="badge success">this browser</span>
              </strong>
              <span class="muted">
                {{ session.ip_address || "unknown address" }} · last seen
                {{ formatMoment(session.last_seen_at) }}
              </span>
            </div>
            <code class="session-id">{{ session.id }}</code>
          </li>
          <li v-if="!sessions.length" class="muted">No sessions listed.</li>
        </ul>

        <p class="field-hint">
          A session ends after {{ authState.sessionIdleMinutes }} minutes idle, and at
          24 hours no matter how active it is.
        </p>
      </div>

      <!-- remembered browsers -->
      <div v-if="devices.length" class="section">
        <div class="section-title">Remembered browsers</div>
        <ul class="session-list">
          <li v-for="device in devices" :key="device.id" class="session-item">
            <div class="session-main">
              <strong>{{ describeAgent(device.user_agent) }}</strong>
              <span class="muted">skips the code until {{ formatMoment(device.expires_at) }}</span>
            </div>
            <code class="session-id">{{ device.id }}</code>
          </li>
        </ul>
        <p class="field-hint">
          These skip the two-factor prompt only. The password is still required.
        </p>
      </div>
    </div>

    <div class="panel-footer">
      <button type="button" class="ghost grow-0" :disabled="busy === 'logout'" @click="signOut">
        Sign out
      </button>
      <span class="grow" />
      <button
        v-if="devices.length"
        type="button"
        class="danger"
        :disabled="busy === 'devices'"
        @click="forgetDevices"
      >
        Forget browsers
      </button>
      <button
        type="button"
        class="danger"
        :disabled="busy === 'sessions' || sessions.length < 2"
        @click="revokeOthers"
      >
        Revoke other sessions
      </button>
    </div>
  </section>
</template>

<style scoped>
.security-form {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.security-actions {
  display: flex;
  gap: var(--space-2);
}

.session-list {
  display: grid;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.session-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  padding: var(--space-2) var(--space-3);
  background: var(--surface-inset);
}

.session-main {
  display: grid;
  flex: 1;
  gap: 2px;
  min-width: 0;
  font-size: 13.5px;
}

.session-main .muted {
  font-size: 12px;
}

.session-id {
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: 11.5px;
}

.security-qr {
  display: grid;
  justify-items: center;
  gap: var(--space-2);
}

/* Light quiet zone in both themes: a phone camera cannot read an inverted QR. */
.security-qr-frame {
  width: 180px;
  border-radius: var(--radius);
  padding: var(--space-2);
  background: #ffffff;
}

.security-qr-frame :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
}

.security-secret {
  display: block;
  border-radius: var(--radius);
  padding: var(--space-2);
  background: var(--surface-inset);
  font-size: 13px;
  letter-spacing: 0.08em;
  overflow-wrap: anywhere;
  user-select: all;
}

.security-code {
  font-family: var(--font-mono);
  font-size: 18px;
  letter-spacing: 0.25em;
  text-align: center;
}

.grow-0 {
  flex: 0 0 auto;
}
</style>
