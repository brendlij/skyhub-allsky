<script setup>
import { computed } from "vue";
import EmptyState from "../components/ui/EmptyState.vue";
import { formatBytes, formatDateTime } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";

const {
  busy,
  environmentTelemetry,
  heaterState,
  latest,
  latestImageUrl,
  loading,
  nodes,
  selectedNode,
  settings,
  setHeaterEnabled,
  startCapture,
  stopCapture,
  storageStats
} = useSkyHub();

const capturing = computed(() => Boolean(settings.value?.capture_enabled));
const onlineCount = computed(() => nodes.value.filter((node) => node.online).length);

function fixed(value, digits = 1) {
  return Number.isFinite(value) ? value.toFixed(digits) : null;
}

const period = computed(() => (latest.value?.period === "day" ? "day" : "night"));

const interval = computed(() => {
  const seconds = settings.value?.[`${period.value}_interval_seconds`];
  return seconds ? `every ${seconds}s` : null;
});

const resolution = computed(() => {
  if (settings.value?.full_resolution) return "Full sensor";
  const { width, height } = settings.value || {};
  return width && height ? `${width}×${height}` : "-";
});

const environment = computed(() => {
  const telemetry = environmentTelemetry.value;
  if (!telemetry) return null;

  return {
    temperature: fixed(telemetry.temperature_c) && `${fixed(telemetry.temperature_c)} °C`,
    humidity: fixed(telemetry.humidity_percent, 0) && `${fixed(telemetry.humidity_percent, 0)}%`,
    dewPoint: fixed(telemetry.dew_point_c) && `${fixed(telemetry.dew_point_c)} °C`,
    pressure: fixed(telemetry.pressure_hpa, 0) && `${fixed(telemetry.pressure_hpa, 0)} hPa`,
    updated: formatDateTime(telemetry.captured_at || telemetry.updated_at)
  };
});

// Dew point within 2°C of the glass is when a heater actually matters.
const dewRisk = computed(() => {
  const telemetry = environmentTelemetry.value;
  if (!telemetry || !Number.isFinite(telemetry.dew_point_c) || !Number.isFinite(telemetry.temperature_c)) {
    return null;
  }

  return telemetry.temperature_c - telemetry.dew_point_c;
});

const heater = computed(() => {
  const state = heaterState.value;
  if (!state) return null;

  return {
    on: Boolean(state.actual_enabled),
    pending: Boolean(state.desired_enabled) !== Boolean(state.actual_enabled),
    desired: state.desired_enabled ? "on" : "off",
    output: state.gpio_pin ? `GPIO ${state.gpio_pin} · ${state.driver || "heater"}` : state.driver || "heater"
  };
});

const exposure = computed(() => {
  const meta = latest.value?.metadata || {};
  const actualExposureMs = meta.actual_exposure_ms;
  const actualAnalogueGain = meta.actual_analogue_gain;
  const actualDigitalGain = meta.actual_digital_gain;
  const ms = actualExposureMs ?? settings.value?.[`${period.value}_exposure_ms`];
  const gain = actualAnalogueGain ?? settings.value?.[`${period.value}_gain`];

  const time = ms
    ? ms >= 1000
      ? `${(ms / 1000).toFixed(2).replace(/\.?0+$/, "")}s`
      : `${Math.round(ms)}ms`
    : null;

  const gains = [
    gain != null ? `${Number(gain).toFixed(1)}x` : null,
    actualDigitalGain != null ? `dg ${Number(actualDigitalGain).toFixed(1)}x` : null
  ].filter(Boolean);

  return { time, gain: gains.join(" · ") || null };
});

// One line of frame facts for the overlay panel — everything else about the
// capture lives in the rail.
const frameSummary = computed(() => {
  const capture = latest.value;
  if (!capture) return "";

  return [
    capture.period,
    capture.width && capture.height ? `${capture.width}×${capture.height}` : null,
    formatBytes(capture.size_bytes)
  ]
    .filter(Boolean)
    .join(" · ");
});
</script>

<template>
  <div v-if="!selectedNode && !loading" class="panel">
    <EmptyState
      icon="⬡"
      title="No node selected"
      message="Start a node with `python skyhub.py node` and it will appear here automatically."
    />
  </div>

  <div v-else class="monitor">
    <section class="stage">
      <div class="image-frame">
        <img v-if="latestImageUrl" :src="latestImageUrl" alt="Latest capture" />
        <div v-else-if="loading" class="skeleton stage-skeleton" />
        <EmptyState
          v-else
          icon="◉"
          title="No captures yet"
          message="The first uploaded frame appears here as soon as a sequence runs."
        />

        <div v-if="latest && latestImageUrl" class="stage-overlay">
          <strong>{{ formatDateTime(latest.captured_at) }}</strong>
          <span>{{ frameSummary }}</span>
        </div>
      </div>

      <p v-if="latest" class="stage-caption truncate" :title="latest.filename">
        {{ latest.filename }}
      </p>
    </section>

    <aside class="rail">
      <button
        v-if="!capturing"
        type="button"
        class="primary block"
        :disabled="!selectedNode || busy.capture"
        @click="startCapture"
      >
        Start capture
      </button>
      <button
        v-else
        type="button"
        class="danger block"
        :disabled="!selectedNode || busy.capture"
        @click="stopCapture"
      >
        Stop capture
      </button>

      <section class="rail-group">
        <h3 class="section-title">Capture</h3>
        <dl class="data-list">
          <div class="data-row">
            <dt>Status</dt>
            <dd :class="capturing ? 'success' : 'faint'" class="data-value">
              {{ capturing ? "Running" : "Idle" }}
            </dd>
          </div>
          <div v-if="interval" class="data-row">
            <dt>Interval</dt>
            <dd>{{ interval }}</dd>
          </div>
          <div class="data-row">
            <dt>Exposure</dt>
            <dd class="mono">{{ exposure.time || "-" }}</dd>
          </div>
          <div v-if="exposure.gain" class="data-row">
            <dt>Gain</dt>
            <dd class="mono">{{ exposure.gain }}</dd>
          </div>
          <div class="data-row">
            <dt>Resolution</dt>
            <dd>{{ resolution }}</dd>
          </div>
        </dl>
      </section>

      <section class="rail-group">
        <h3 class="section-title">Environment</h3>
        <p v-if="!environment" class="rail-empty">Sensor has not reported yet.</p>
        <dl v-else class="data-list">
          <div v-if="environment.temperature" class="data-row">
            <dt>Temperature</dt>
            <dd>{{ environment.temperature }}</dd>
          </div>
          <div v-if="environment.humidity" class="data-row">
            <dt>Humidity</dt>
            <dd>{{ environment.humidity }}</dd>
          </div>
          <div v-if="environment.dewPoint" class="data-row">
            <dt>Dew point</dt>
            <dd class="data-value" :class="dewRisk !== null && dewRisk < 2 ? 'warning' : ''">
              {{ environment.dewPoint }}
            </dd>
          </div>
          <div v-if="environment.pressure" class="data-row">
            <dt>Pressure</dt>
            <dd>{{ environment.pressure }}</dd>
          </div>
          <div v-if="environment.updated" class="data-row">
            <dt>Updated</dt>
            <dd class="data-value faint">{{ environment.updated }}</dd>
          </div>
        </dl>
      </section>

      <section class="rail-group">
        <div class="rail-group-head">
          <h3 class="section-title">Heater</h3>
          <button
            type="button"
            class="sm"
            :disabled="!selectedNode || busy.heater"
            @click="setHeaterEnabled(!heaterState?.desired_enabled)"
          >
            {{ heaterState?.desired_enabled ? "Turn off" : "Turn on" }}
          </button>
        </div>
        <p v-if="!heater" class="rail-empty">No heater state yet.</p>
        <dl v-else class="data-list">
          <div class="data-row">
            <dt>State</dt>
            <dd class="data-value" :class="heater.on ? 'success' : 'faint'">
              {{ heater.on ? "On" : "Off" }}
            </dd>
          </div>
          <div v-if="heater.pending" class="data-row">
            <dt>Pending</dt>
            <dd class="data-value warning">Desired {{ heater.desired }}</dd>
          </div>
          <div class="data-row">
            <dt>Output</dt>
            <dd class="data-value mono">{{ heater.output }}</dd>
          </div>
        </dl>
      </section>

      <section class="rail-group">
        <h3 class="section-title">Node</h3>
        <dl class="data-list">
          <div v-if="selectedNode" class="data-row">
            <dt>Selected</dt>
            <dd class="data-value mono">{{ selectedNode.node_id }}</dd>
          </div>
          <div class="data-row">
            <dt>Online</dt>
            <dd class="data-value" :class="onlineCount ? 'success' : 'danger'">
              {{ onlineCount }}/{{ nodes.length }}
            </dd>
          </div>
          <div class="data-row">
            <dt>Captures</dt>
            <dd>{{ formatBytes(storageStats?.capture_storage_bytes) }}</dd>
          </div>
          <div v-if="storageStats" class="data-row">
            <dt>Disk free</dt>
            <dd>{{ formatBytes(storageStats.disk_free_bytes) }}</dd>
          </div>
        </dl>
      </section>
    </aside>
  </div>
</template>
