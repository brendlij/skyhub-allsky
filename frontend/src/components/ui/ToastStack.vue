<script setup>
import { useToasts } from "../../composables/useToasts";

const { toasts, dismissToast } = useToasts();

const ICONS = { success: "✓", error: "!", warning: "!", info: "i" };
</script>

<template>
  <div class="toast-stack" role="region" aria-label="Notifications" aria-live="polite">
    <TransitionGroup name="toast">
      <div v-for="toast in toasts" :key="toast.id" class="toast" :class="toast.tone">
        <span class="toast-icon" aria-hidden="true">{{ ICONS[toast.tone] || "i" }}</span>
        <div class="toast-body">
          <strong v-if="toast.title">{{ toast.title }}</strong>
          <span>{{ toast.message }}</span>
        </div>
        <span v-if="toast.count > 1" class="toast-count">{{ toast.count }}</span>
        <button
          type="button"
          class="toast-close"
          aria-label="Dismiss notification"
          @click="dismissToast(toast.id)"
        >
          ×
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-stack {
  position: fixed;
  z-index: 200;
  right: var(--space-4);
  bottom: var(--space-4);
  display: grid;
  gap: var(--space-2);
  width: min(380px, calc(100vw - var(--space-5)));
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--text-faint);
  border-radius: var(--radius);
  padding: 10px 11px;
  background: var(--surface-raised);
  box-shadow: var(--shadow-lg);
  pointer-events: auto;
}

.toast.success { border-left-color: var(--success); }
.toast.error { border-left-color: var(--danger); }
.toast.warning { border-left-color: var(--warning); }
.toast.info { border-left-color: var(--accent); }

.toast-icon {
  display: grid;
  width: 18px;
  height: 18px;
  flex: none;
  place-items: center;
  margin-top: 1px;
  border-radius: var(--radius-full);
  font-size: 11px;
  font-weight: 800;
}

.success .toast-icon { background: var(--success-soft); color: var(--success); }
.error .toast-icon { background: var(--danger-soft); color: var(--danger); }
.warning .toast-icon { background: var(--warning-soft); color: var(--warning); }
.info .toast-icon { background: var(--accent-soft); color: var(--accent); }

.toast-body {
  display: grid;
  flex: 1;
  gap: 2px;
  min-width: 0;
  font-size: 12.5px;
  line-height: 1.45;
}

.toast-body strong {
  font-size: 12.5px;
}

.toast-body span {
  overflow-wrap: anywhere;
  color: var(--text-muted);
}

.toast-count {
  flex: none;
  border-radius: var(--radius-full);
  padding: 1px 7px;
  background: var(--surface-active);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.toast-close {
  width: 22px;
  min-height: 22px;
  flex: none;
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--text-faint);
  font-size: 16px;
  line-height: 1;
}

.toast-close:hover {
  background: var(--surface-hover);
  color: var(--text);
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity var(--transition), transform var(--transition);
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateX(14px);
}
</style>
