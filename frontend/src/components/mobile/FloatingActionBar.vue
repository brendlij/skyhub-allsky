<script setup>
/* A row of circular actions that floats over the image.
 *
 * Actions: { id, label, icon, tone?, disabled?, primary? }. The primary one is
 * wider and carries its label, because "Start capture" is not a guessable glyph.
 */

defineProps({
  actions: { type: Array, required: true }
});

const emit = defineEmits(["select"]);
</script>

<template>
  <div class="fab-bar">
    <button
      v-for="action in actions"
      :key="action.id"
      type="button"
      class="fab"
      :class="[action.tone, { wide: action.primary }]"
      :disabled="action.disabled"
      :aria-label="action.label"
      :title="action.label"
      @click.stop="emit('select', action.id)"
    >
      <span class="fab-icon" aria-hidden="true">{{ action.icon }}</span>
      <span v-if="action.primary" class="fab-label">{{ action.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.fab-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
}

.fab {
  display: inline-flex;
  width: var(--tap-target);
  height: var(--tap-target);
  min-height: var(--tap-target);
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  border: 1px solid var(--overlay-border);
  border-radius: var(--radius-full);
  padding: 0;
  background: var(--overlay-panel);
  color: var(--overlay-text);
  backdrop-filter: blur(16px);
  box-shadow: var(--shadow);
  transition: transform var(--motion-fast), background var(--motion-fast);
}

.fab:hover:not(:disabled) {
  border-color: var(--overlay-border);
  background: var(--overlay-panel);
}

.fab:active:not(:disabled) {
  transform: scale(0.92);
}

.fab.wide {
  width: auto;
  padding: 0 var(--space-5);
}

.fab.accent {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-text);
}

.fab.danger {
  border-color: transparent;
  background: var(--danger);
  color: #ffffff;
}

.fab-icon {
  font-size: 17px;
  line-height: 1;
}

.fab-label {
  font-size: 14px;
  font-weight: 600;
}
</style>
