<script setup>
import { computed, ref } from "vue";
import AccordionCard from "../components/mobile/AccordionCard.vue";
import BottomSheet from "../components/mobile/BottomSheet.vue";
import CameraStage from "../components/mobile/CameraStage.vue";
import MetricGrid from "../components/mobile/MetricGrid.vue";
import SectionCard from "../components/mobile/SectionCard.vue";
import ToggleSwitch from "../components/mobile/ToggleSwitch.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import { formatBytes, formatDateTime } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";
import { useViewport } from "../composables/useViewport";

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

const { isMobile } = useViewport();

const detailsOpen = ref(false);

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

const dewTone = computed(() => (dewRisk.value !== null && dewRisk.value < 2 ? "warning" : ""));

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

/* ---------- Mobile presentation ---------- */

// Chips ride on the image, so they carry only what you would want to know
// without looking away from the sky.
const chips = computed(() => {
  const list = [{
    value: capturing.value ? "Live" : "Idle",
    tone: capturing.value ? "success" : "",
    dot: true,
    pulse: capturing.value
  }];

  if (exposure.value.time) list.push({ value: exposure.value.time, label: "exposure" });
  if (environment.value?.temperature) list.push({ value: environment.value.temperature, label: "temperature" });

  return list.map((chip) => ({ ...chip, label: chip.label || chip.value }));
});

function metric(label, value, extra = {}) {
  return value ? { label, value, ...extra } : null;
}

const statusMetrics = computed(() => [
  metric("Status", capturing.value ? "Running" : "Idle", { tone: capturing.value ? "success" : "faint" }),
  metric("Interval", interval.value),
  metric("Exposure", exposure.value.time || "-", { mono: true }),
  metric("Gain", exposure.value.gain, { mono: true }),
  metric("Resolution", resolution.value),
  metric("Last frame", latest.value ? formatDateTime(latest.value.captured_at) : null)
].filter(Boolean));

const environmentMetrics = computed(() => [
  metric("Temperature", environment.value?.temperature),
  metric("Humidity", environment.value?.humidity),
  metric("Dew point", environment.value?.dewPoint, { tone: dewTone.value }),
  metric("Pressure", environment.value?.pressure),
  metric("Updated", environment.value?.updated, { tone: "faint" })
].filter(Boolean));

const heaterMetrics = computed(() => [
  metric("State", heater.value ? (heater.value.on ? "On" : "Off") : "Unknown", {
    tone: heater.value?.on ? "success" : "faint"
  }),
  heater.value?.pending ? { label: "Pending", value: `Desired ${heater.value.desired}`, tone: "warning" } : null,
  metric("Output", heater.value?.output, { mono: true })
].filter(Boolean));

const nodeMetrics = computed(() => [
  metric("Node", selectedNode.value?.node_id, { mono: true }),
  metric("Online", `${onlineCount.value}/${nodes.value.length}`, {
    tone: onlineCount.value ? "success" : "danger"
  }),
  metric("Captures", formatBytes(storageStats.value?.capture_storage_bytes)),
  metric("Disk free", storageStats.value ? formatBytes(storageStats.value.disk_free_bytes) : null)
].filter(Boolean));

const environmentSummary = computed(() => environment.value?.temperature || "No data");
</script>

<template>
  <!-- ---------- Mobile: the live view is the page ---------- -->
  <div v-if="isMobile" class="monitor-mobile">
    <SectionCard v-if="!selectedNode && !loading">
      <EmptyState
        icon="⬡"
        title="No node selected"
        message="Start a node with `python skyhub.py node` and it will appear here automatically."
      />
    </SectionCard>

    <template v-else>
      <CameraStage :chips="chips" @open-details="detailsOpen = true" />

      <SectionCard title="Status" :note="latest ? latest.period : ''">
        <MetricGrid :items="statusMetrics" />
      </SectionCard>

      <AccordionCard title="Environment" :summary="environmentSummary" :tone="dewTone">
        <MetricGrid v-if="environmentMetrics.length" :items="environmentMetrics" />
        <p v-else class="monitor-empty">Sensor has not reported yet.</p>
      </AccordionCard>

      <SectionCard title="Heater" :note="heater?.output || ''">
        <template #action>
          <ToggleSwitch
            :model-value="Boolean(heaterState?.desired_enabled)"
            label="Heater"
            :disabled="!selectedNode"
            :busy="busy.heater"
            @update:model-value="setHeaterEnabled($event)"
          />
        </template>
        <MetricGrid :items="heaterMetrics" />
      </SectionCard>

      <AccordionCard title="Node & storage" :summary="`${onlineCount}/${nodes.length}`">
        <MetricGrid :items="nodeMetrics" />
      </AccordionCard>
    </template>

    <!-- Long-pressing the image opens everything at once, without leaving the feed. -->
    <BottomSheet :open="detailsOpen" title="Telemetry" @close="detailsOpen = false">
      <div class="monitor-sheet">
        <section>
          <h3>Capture</h3>
          <MetricGrid :items="statusMetrics" />
        </section>
        <section v-if="environmentMetrics.length">
          <h3>Environment</h3>
          <MetricGrid :items="environmentMetrics" />
        </section>
        <section>
          <h3>Heater</h3>
          <MetricGrid :items="heaterMetrics" />
        </section>
        <section>
          <h3>Node &amp; storage</h3>
          <MetricGrid :items="nodeMetrics" />
        </section>
      </div>
    </BottomSheet>
  </div>

  <!-- ---------- Desktop: image left, rail right ---------- -->
  <template v-else>
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
              <dd class="data-value" :class="dewTone">{{ environment.dewPoint }}</dd>
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
</template>

<style scoped>
.monitor-mobile {
  display: grid;
  align-content: start;
  gap: var(--space-4);
}

.monitor-empty {
  color: var(--text-faint);
  font-size: 14px;
}

.monitor-sheet {
  display: grid;
  gap: var(--space-6);
}

.monitor-sheet h3 {
  padding-bottom: var(--space-3);
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 500;
}
</style>
