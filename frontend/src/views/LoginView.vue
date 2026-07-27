<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  beginSetup,
  confirmSetup,
  login,
  refreshAuthState,
  submitTotp,
  authState
} from "../api/auth";

/* One page, four steps, because they are the same conversation:
 *
 *   setup     -> claim the account, using the token printed in the server log
 *   enrol     -> scan the QR, prove it with a code
 *   password  -> username and password
 *   code      -> the six digit code
 *
 * Keeping them in one component means the transition between them is state, not
 * navigation: a half-finished login never leaves a URL someone can bookmark or
 * a back button that lands between two steps.
 */

const router = useRouter();
const route = useRoute();

const step = ref("password");
const busy = ref(false);
const error = ref(null);
const retryAfter = ref(0);

const setupToken = ref("");
const username = ref("");
const password = ref("");
const confirmPassword = ref("");
const code = ref("");
const rememberDevice = ref(false);

const enrolment = ref(null);
const secretVisible = ref(false);

const usernameInput = ref(null);
const codeInput = ref(null);
const setupTokenInput = ref(null);

const isSetup = computed(() => step.value === "setup");
const isEnrol = computed(() => step.value === "enrol");
const isCode = computed(() => step.value === "code");

const heading = computed(() => ({
  setup: "Set up SkyHub",
  enrol: "Add your authenticator",
  password: "Sign in",
  code: "Two-factor code"
}[step.value]));

const canSubmit = computed(() => {
  if (busy.value || retryAfter.value > 0) return false;
  if (isSetup.value) {
    return Boolean(setupToken.value.trim() && username.value.trim() && password.value)
      && password.value === confirmPassword.value;
  }
  if (isEnrol.value || isCode.value) return code.value.replace(/\D/g, "").length === 6;

  return Boolean(username.value.trim() && password.value);
});

const passwordMismatch = computed(() => (
  isSetup.value && confirmPassword.value.length > 0 && password.value !== confirmPassword.value
));

onMounted(async () => {
  const state = await refreshAuthState();

  if (state.setupRequired) {
    step.value = "setup";
  } else if (state.totpPending) {
    // A reload during the code step: the pending session is still on the server.
    step.value = "code";
  }

  await focusFirstField();
});

watch(step, focusFirstField);

async function focusFirstField() {
  await nextTick();

  if (isSetup.value) setupTokenInput.value?.focus();
  else if (isEnrol.value || isCode.value) codeInput.value?.focus();
  else usernameInput.value?.focus();
}

/* A 429 carries the seconds to wait. Counting it down in the button is kinder
 * than an error that looks permanent, and makes the backoff legible rather than
 * feeling like a broken form. */
function startCountdown(seconds) {
  retryAfter.value = seconds;

  const timer = window.setInterval(() => {
    retryAfter.value -= 1;

    if (retryAfter.value <= 0) window.clearInterval(timer);
  }, 1000);
}

function handleError(caught) {
  error.value = caught.message || "Something went wrong.";

  if (caught.status === 429 && caught.retryAfter > 0) startCountdown(caught.retryAfter);
}

async function finish() {
  await refreshAuthState();

  const target = typeof route.query.redirect === "string" ? route.query.redirect : "/monitor";

  // Never bounce back to the login page itself, however we got here.
  router.replace(target.startsWith("/login") ? "/monitor" : target);
}

async function submit() {
  if (!canSubmit.value) return;

  busy.value = true;
  error.value = null;

  try {
    if (isSetup.value) {
      enrolment.value = await beginSetup(setupToken.value.trim(), username.value.trim(), password.value);
      password.value = "";
      confirmPassword.value = "";
      setupToken.value = "";
      step.value = "enrol";

    } else if (isEnrol.value) {
      await confirmSetup(code.value, rememberDevice.value);
      await finish();

    } else if (isCode.value) {
      await submitTotp(code.value, rememberDevice.value);
      await finish();

    } else {
      const result = await login(username.value.trim(), password.value);
      password.value = "";

      if (result.status === "authenticated") {
        await finish();
      } else if (result.status === "totp_enrolment_required") {
        // The account exists but enrolment was never finished. Pick it up here
        // rather than presenting a code prompt nothing can answer.
        enrolment.value = result;
        step.value = "enrol";
      } else {
        code.value = "";
        step.value = "code";
      }
    }
  } catch (caught) {
    handleError(caught);
    code.value = "";
  } finally {
    busy.value = false;
  }
}

function restart() {
  step.value = authState.value.setupRequired ? "setup" : "password";
  code.value = "";
  password.value = "";
  error.value = null;
}
</script>

<template>
  <div class="auth-page">
    <form class="auth-card" @submit.prevent="submit">
      <div class="auth-brand">
        <span class="auth-mark" aria-hidden="true">◐</span>
        <div>
          <h1>{{ heading }}</h1>
          <p v-if="isSetup">
            This server has no account yet. The one-time setup token is printed in the
            server log and saved to <code>data/setup-token.txt</code>.
          </p>
          <p v-else-if="isEnrol">
            Scan this with Google Authenticator, Aegis, 1Password, Bitwarden — anything
            that does TOTP — then enter the code it shows.
          </p>
          <p v-else-if="isCode">
            Enter the six-digit code from your authenticator app.
          </p>
          <p v-else>SkyHub allsky</p>
        </div>
      </div>

      <p v-if="error" class="callout danger" role="alert">{{ error }}</p>

      <!-- first run: claim the account -->
      <template v-if="isSetup">
        <label class="field">
          <span>Setup token</span>
          <input
            ref="setupTokenInput"
            v-model="setupToken"
            type="text"
            autocomplete="off"
            spellcheck="false"
            placeholder="from the server log"
            :disabled="busy"
          />
        </label>
        <label class="field">
          <span>Username</span>
          <input v-model="username" type="text" autocomplete="username" :disabled="busy" />
        </label>
        <label class="field">
          <span>Password <em class="field-unit">at least 12 characters</em></span>
          <input v-model="password" type="password" autocomplete="new-password" :disabled="busy" />
        </label>
        <label class="field">
          <span>Repeat password</span>
          <input v-model="confirmPassword" type="password" autocomplete="new-password" :disabled="busy" />
        </label>
        <p v-if="passwordMismatch" class="field-hint danger-text">The passwords do not match.</p>
      </template>

      <!-- enrolment: the QR and its code -->
      <template v-else-if="isEnrol">
        <div v-if="enrolment" class="auth-qr">
          <!-- Inline SVG from the server, so the secret never travels as a second
               request that could be logged or cached. v-html is safe on it: the
               payload is a QR encoder's own <path> output, and the username is
               encoded into the modules rather than written into the markup, so
               there is no route from user input to an element here. -->
          <div class="auth-qr-frame" v-html="enrolment.qr_svg" />
          <button type="button" class="ghost sm" @click="secretVisible = !secretVisible">
            {{ secretVisible ? "Hide" : "Can't scan? Show the key" }}
          </button>
          <code v-if="secretVisible" class="auth-secret">{{ enrolment.secret }}</code>
        </div>

        <label class="field">
          <span>Code from your app</span>
          <input
            ref="codeInput"
            v-model="code"
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="7"
            placeholder="000000"
            class="auth-code"
            :disabled="busy"
          />
        </label>

        <p class="callout warning">
          Save the key somewhere safe before continuing. Lose the authenticator without
          it and the only way back in is the database on the server.
        </p>
      </template>

      <!-- second factor -->
      <template v-else-if="isCode">
        <label class="field">
          <span>Authentication code</span>
          <input
            ref="codeInput"
            v-model="code"
            type="text"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="7"
            placeholder="000000"
            class="auth-code"
            :disabled="busy"
          />
        </label>
        <label class="check">
          <input v-model="rememberDevice" type="checkbox" :disabled="busy" />
          Remember this browser for 30 days
        </label>
        <p class="field-hint">
          Remembering skips this code on this browser only. The password is still
          required every time.
        </p>
      </template>

      <!-- username and password -->
      <template v-else>
        <label class="field">
          <span>Username</span>
          <input
            ref="usernameInput"
            v-model="username"
            type="text"
            autocomplete="username"
            :disabled="busy"
          />
        </label>
        <label class="field">
          <span>Password</span>
          <input v-model="password" type="password" autocomplete="current-password" :disabled="busy" />
        </label>
      </template>

      <button type="submit" class="primary auth-submit" :disabled="!canSubmit">
        <template v-if="retryAfter > 0">Try again in {{ retryAfter }}s</template>
        <template v-else-if="busy">Working…</template>
        <template v-else-if="isSetup">Create account</template>
        <template v-else-if="isEnrol">Confirm and sign in</template>
        <template v-else-if="isCode">Verify</template>
        <template v-else>Sign in</template>
      </button>

      <button v-if="isCode" type="button" class="ghost sm" @click="restart">
        Start over
      </button>
    </form>
  </div>
</template>

<style scoped>
.auth-page {
  display: grid;
  place-items: center;
  min-height: 100vh;
  min-height: 100dvh;
  padding: var(--space-4);
  background: var(--bg);
}

.auth-card {
  display: grid;
  gap: var(--space-4);
  width: min(420px, 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  padding: var(--space-6);
  background: var(--surface);
  box-shadow: var(--shadow-lg);
}

.auth-brand {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.auth-mark {
  color: var(--accent);
  font-size: 28px;
  line-height: 1;
}

.auth-brand h1 {
  margin: 0;
  font-size: 19px;
}

.auth-brand p {
  margin: var(--space-1) 0 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
}

/* A code field is read digit by digit, out loud, off a phone. Wide tracking and
 * a monospace face make a transcription error visible before it is submitted. */
.auth-code {
  font-family: var(--font-mono);
  font-size: 20px;
  letter-spacing: 0.3em;
  text-align: center;
}

.auth-qr {
  display: grid;
  justify-items: center;
  gap: var(--space-2);
}

/* The QR is scanned off a screen by a phone camera, so it needs a light quiet
 * zone in both themes - a dark-mode QR is unreadable to most scanners. */
.auth-qr-frame {
  width: 200px;
  border-radius: var(--radius);
  padding: var(--space-2);
  background: #ffffff;
}

.auth-qr-frame :deep(svg) {
  display: block;
  width: 100%;
  height: auto;
}

.auth-secret {
  display: block;
  border-radius: var(--radius);
  padding: var(--space-2);
  background: var(--surface-inset);
  font-size: 13px;
  letter-spacing: 0.08em;
  overflow-wrap: anywhere;
  user-select: all;
}

.auth-submit {
  min-height: 42px;
}

.danger-text {
  color: var(--danger);
}
</style>
