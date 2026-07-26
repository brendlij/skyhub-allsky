<script setup>
import { nextTick, ref, watch } from "vue";
import { apiKey, apiKeyRequired, setApiKey } from "../../api/skyhub";

// Only ever shown when the server answered 401, so an install without a key
// configured never sees this.
const draft = ref(apiKey.value);
const input = ref(null);

watch(apiKeyRequired, async (required) => {
  if (!required) return;

  draft.value = apiKey.value;
  await nextTick();
  input.value?.focus();
});

function submit() {
  if (!draft.value.trim()) return;

  setApiKey(draft.value);
  // A reload is the honest way to re-run every load that failed, including the
  // dashboard WebSocket, which cannot be re-authenticated in place.
  window.location.reload();
}
</script>

<template>
  <Transition name="dialog">
    <div
      v-if="apiKeyRequired"
      class="dialog-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="API key required"
    >
      <form class="dialog" @submit.prevent="submit">
        <h2>API key required</h2>
        <p>
          This server is protected by an API key. Paste the value of
          <code>SKYHUB_SERVER_API_KEY</code> to continue - it is kept in this
          browser only.
        </p>
        <label class="field">
          <span>API key</span>
          <input
            ref="input"
            v-model="draft"
            type="password"
            autocomplete="current-password"
            placeholder="skyhub_..."
          />
        </label>
        <div class="dialog-actions">
          <button type="submit" class="primary" :disabled="!draft.trim()">Save and reload</button>
        </div>
      </form>
    </div>
  </Transition>
</template>

<style scoped>
.dialog-backdrop {
  position: fixed;
  z-index: 400;
  inset: 0;
  display: grid;
  place-items: center;
  padding: var(--space-4);
  background: #04070db8;
  backdrop-filter: blur(3px);
}

.dialog {
  display: grid;
  gap: var(--space-3);
  width: min(420px, 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-6);
  background: var(--surface-raised);
  box-shadow: var(--shadow-lg);
}

.dialog p {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.55;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
}

.dialog-enter-active,
.dialog-leave-active {
  transition: opacity var(--transition);
}

.dialog-enter-from,
.dialog-leave-to {
  opacity: 0;
}
</style>
