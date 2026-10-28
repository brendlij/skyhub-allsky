<script setup>
import { computed } from "vue";

/* One labelled slider: name on the left, current value on the right, a single track
 * underneath. The track is the control - the old version stacked a decorative
 * gradient bar on top of a separate native slider, which read as two controls. */

const props = defineProps({
  label: { type: String, required: true },
  hint: { type: String, default: "" },
  modelValue: { type: Number, required: true },
  min: { type: Number, required: true },
  max: { type: Number, required: true },
  step: { type: Number, default: 1 },
  /* What the value pill shows; falls back to the raw number. */
  display: { type: String, default: "" },
  /* "plain" fills up to the thumb; "tint" and "warmth" show the colour range the
   * slider moves through instead of a fill. */
  track: { type: String, default: "plain" },
  disabled: { type: Boolean, default: false }
});

const emit = defineEmits(["update:modelValue"]);

const fill = computed(() => {
  const span = props.max - props.min;

  return span > 0 ? Math.min(100, Math.max(0, ((props.modelValue - props.min) / span) * 100)) : 0;
});

const shown = computed(() => props.display || String(props.modelValue));
</script>

<template>
  <div class="slider-field" :class="{ disabled }">
    <div class="slider-head">
      <span class="slider-label">
        {{ label }}
        <small v-if="hint">{{ hint }}</small>
      </span>
      <span class="slider-value">{{ shown }}</span>
    </div>

    <input
      class="slider"
      :class="`track-${track}`"
      type="range"
      :min="min"
      :max="max"
      :step="step"
      :value="modelValue"
      :disabled="disabled"
      :aria-label="label"
      :style="{ '--fill': `${fill}%` }"
      @input="emit('update:modelValue', Number($event.target.value))"
    />
  </div>
</template>
