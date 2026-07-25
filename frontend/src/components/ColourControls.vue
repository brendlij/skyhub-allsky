<script setup>
import { computed } from "vue";

const props = defineProps({
  settings: { type: Object, required: true },
  period: { type: String, required: true }
});

const key = (name) => `${props.period}_${name}`;

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

const autoWhiteBalance = computed({
  get: () => Boolean(props.settings[key("auto_white_balance")]),
  set: (value) => { props.settings[key("auto_white_balance")] = value; }
});

const red = computed(() => Number(props.settings[key("wb_red")]) || 1);
const blue = computed(() => Number(props.settings[key("wb_blue")]) || 1);

function setGains(nextRed, nextBlue) {
  props.settings[key("wb_red")] = Math.round(clamp(nextRed, 0.1, 8) * 100) / 100;
  props.settings[key("wb_blue")] = Math.round(clamp(nextBlue, 0.1, 8) * 100) / 100;
}

/* libcamera's ColourGains scale red and blue against green, which is fixed at 1.
 * Raising both together pulls the image toward magenta and lowering both pushes
 * it green - that is the tint axis. Their ratio is the warm/cool axis. Both
 * directions are an exact bijection with the stored gains. */
const tint = computed({
  get: () => Math.sqrt(red.value * blue.value),
  set: (value) => {
    const warmth = Math.sqrt(red.value / blue.value);
    setGains(value * warmth, value / warmth);
  }
});

const warmth = computed({
  get: () => Math.sqrt(red.value / blue.value),
  set: (value) => {
    const level = Math.sqrt(red.value * blue.value);
    setGains(level * value, level / value);
  }
});

const saturation = computed({
  get: () => Number(props.settings[key("saturation")] ?? 1),
  set: (value) => { props.settings[key("saturation")] = Number(value); }
});

const hue = computed({
  get: () => Number(props.settings[key("hue")] ?? 0),
  set: (value) => { props.settings[key("hue")] = Number(value); }
});

const tintLabel = computed(() => {
  if (tint.value > 2.05) return "magenta";
  if (tint.value < 1.55) return "green";
  return "neutral";
});

function resetNeutral() {
  setGains(1, 1);
  saturation.value = 1;
  hue.value = 0;
}
</script>

<template>
  <div class="colour-controls">
    <div class="section-title">Colour</div>

    <label class="check">
      <input v-model="autoWhiteBalance" type="checkbox" />
      Auto white balance
    </label>

    <p v-if="autoWhiteBalance" class="callout warning">
      Auto white balance has no reliable reference under a night sky and drifts
      between green and magenta between frames. Switch it off for stable colour.
    </p>

    <fieldset class="colour-sliders" :disabled="autoWhiteBalance">
      <div class="field">
        <span class="field-label">
          Tint <small class="faint">green ↔ magenta</small>
          <em class="field-value">{{ tintLabel }}</em>
        </span>
        <div class="colour-swatch tint-track" aria-hidden="true" />
        <input v-model.number="tint" type="range" min="0.6" max="3.5" step="0.01" />
      </div>

      <div class="field">
        <span class="field-label">
          Warmth <small class="faint">blue ↔ amber</small>
          <em class="field-value">{{ warmth.toFixed(2) }}</em>
        </span>
        <div class="colour-swatch warmth-track" aria-hidden="true" />
        <input v-model.number="warmth" type="range" min="0.4" max="2.5" step="0.01" />
      </div>

      <div class="colour-gains">
        <label class="field">
          <span>Red gain</span>
          <input
            :value="red"
            type="number"
            step="0.05"
            min="0.1"
            max="8"
            @input="setGains(Number($event.target.value), blue)"
          />
        </label>
        <label class="field">
          <span>Blue gain</span>
          <input
            :value="blue"
            type="number"
            step="0.05"
            min="0.1"
            max="8"
            @input="setGains(red, Number($event.target.value))"
          />
        </label>
      </div>
    </fieldset>

    <div class="field">
      <span class="field-label">
        Saturation
        <em class="field-value">{{ saturation.toFixed(2) }}</em>
      </span>
      <input v-model.number="saturation" type="range" min="0" max="2" step="0.05" />
    </div>

    <div class="field">
      <span class="field-label">
        Hue shift
        <em class="field-value">{{ hue.toFixed(0) }}°</em>
      </span>
      <input v-model.number="hue" type="range" min="-180" max="180" step="1" />
    </div>

    <p class="field-hint">
      Tint, warmth and saturation are applied by the camera. Hue is applied when the
      capture is rendered, so it leaves the stored original untouched — prefer tint
      for fixing a colour cast.
    </p>

    <button type="button" class="sm" @click="resetNeutral">Reset colour</button>
  </div>
</template>
