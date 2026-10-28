<script setup>
import { computed } from "vue";
import SliderField from "./ui/SliderField.vue";

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
  if (tint.value > 2.05) return "Magenta";
  if (tint.value < 1.55) return "Green";
  return "Neutral";
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
      <SliderField
        v-model="tint"
        label="Tint"
        hint="green to magenta"
        :display="tintLabel"
        :min="0.6"
        :max="3.5"
        :step="0.01"
        track="tint"
      />
      <SliderField
        v-model="warmth"
        label="Warmth"
        hint="blue to amber"
        :display="warmth.toFixed(2)"
        :min="0.4"
        :max="2.5"
        :step="0.01"
        track="warmth"
      />
    </fieldset>

    <SliderField
      v-model="saturation"
      label="Saturation"
      :display="saturation.toFixed(2)"
      :min="0"
      :max="2"
      :step="0.05"
    />

    <SliderField
      v-model="hue"
      label="Hue shift"
      :display="`${hue.toFixed(0)}°`"
      :min="-180"
      :max="180"
      :step="1"
    />

    <details class="advanced">
      <summary>Raw channel gains</summary>
      <fieldset class="colour-gains" :disabled="autoWhiteBalance">
        <label class="field">
          <span>Red</span>
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
          <span>Blue</span>
          <input
            :value="blue"
            type="number"
            step="0.05"
            min="0.1"
            max="8"
            @input="setGains(red, Number($event.target.value))"
          />
        </label>
      </fieldset>
    </details>

    <p class="field-hint">
      Tint, warmth and saturation are applied by the camera. Hue is applied when the
      capture is rendered, so it leaves the stored original untouched. Prefer tint for
      fixing a colour cast.
    </p>

    <button type="button" class="sm" @click="resetNeutral">Reset colour</button>
  </div>
</template>
