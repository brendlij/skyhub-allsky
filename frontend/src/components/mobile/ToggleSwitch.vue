<script setup>
/* A switch, not a button: it shows the current state as well as offering the
 * change, which is what you want for something physical like a heater. */

defineProps({
  modelValue: { type: Boolean, default: false },
  label: { type: String, required: true },
  disabled: { type: Boolean, default: false },
  busy: { type: Boolean, default: false }
});

const emit = defineEmits(["update:modelValue"]);
</script>

<template>
  <button
    type="button"
    class="switch"
    role="switch"
    :aria-checked="modelValue"
    :aria-label="label"
    :disabled="disabled || busy"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span class="switch-track" :class="{ on: modelValue }">
      <span class="switch-knob" />
    </span>
  </button>
</template>

<style scoped>
.switch {
  display: grid;
  width: var(--tap-target);
  min-height: var(--tap-target);
  place-items: center;
  border: 0;
  padding: 0;
  background: transparent;
}

.switch:hover:not(:disabled) {
  border-color: transparent;
  background: transparent;
}

.switch-track {
  display: flex;
  width: 46px;
  height: 28px;
  align-items: center;
  border-radius: var(--radius-full);
  padding: 2px;
  background: var(--surface-active);
  transition: background var(--motion-fast);
}

.switch-track.on {
  background: var(--success);
}

.switch-knob {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-full);
  background: #ffffff;
  box-shadow: var(--shadow-sm);
  transition: transform var(--motion-fast);
}

.switch-track.on .switch-knob {
  transform: translateX(18px);
}
</style>
