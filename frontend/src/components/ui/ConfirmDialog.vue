<script setup>
import { nextTick, ref, watch } from "vue";
import { useConfirm } from "../../composables/useConfirm";

const { request, accept, cancel } = useConfirm();
const confirmButton = ref(null);

// Focus the confirm button when the dialog opens so Enter and Escape both work
// without reaching for the mouse.
watch(request, async (value) => {
  if (!value) return;
  await nextTick();
  confirmButton.value?.focus();
});

function onKeydown(event) {
  if (event.key === "Escape") cancel();
}
</script>

<template>
  <Transition name="dialog">
    <div
      v-if="request"
      class="dialog-backdrop"
      role="dialog"
      aria-modal="true"
      :aria-label="request.title"
      @click.self="cancel"
      @keydown="onKeydown"
    >
      <div class="dialog">
        <h2>{{ request.title }}</h2>
        <p v-if="request.message">{{ request.message }}</p>
        <div class="dialog-actions">
          <button type="button" @click="cancel">{{ request.cancelLabel }}</button>
          <button
            ref="confirmButton"
            type="button"
            :class="request.tone === 'danger' ? 'danger' : 'primary'"
            @click="accept"
          >
            {{ request.confirmLabel }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.dialog-backdrop {
  position: fixed;
  z-index: 300;
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
  padding: var(--space-5);
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
  gap: var(--space-2);
  margin-top: var(--space-1);
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
