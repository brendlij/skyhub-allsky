<script setup>
import { computed } from "vue";
import EmptyState from "../components/ui/EmptyState.vue";
import StatTile from "../components/ui/StatTile.vue";
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

const environment = computed(() => {
  const telemetry = environmentTelemetry.value;
  if (!telemetry) return { value: "No data", detail: "Sensor has not reported yet" };

  const detail = [
    fixed(telemetry.dew_point_c) && `dew ${fixed(telemetry.dew_point_c)}°C`,
    fixed(telemetry.pressure_hpa, 0) && `${fixed(telemetry.pressure_hpa, 0)} hPa`,
    formatDateTime(telemetry.captured_at || telemetry.updated_at)
  ].filter(Boolean);

  return {
    value: `${fixed(telemetry.temperature_c)}°C · ${fixed(telemetry.humidity_percent, 0)}%`,
    detail: detail.join(" · ")
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
  if (!state) return { value: "Unknown", detail: "No heater state yet", tone: "" };

  const mismatch = Boolean(state.desired_enabled) !== Boolean(state.actual_enabled);

  return {
    value: state.actual_enabled ? "On" : "Off",
    detail: mismatch
      ? `Desired ${state.desired_enabled ? "on" : "off"} — not applied yet`
      : state.gpio_pin ? `GPIO ${state.gpio_pin} · ${state.driver || "heater"}` : state.driver || "heater",
    tone: mismatch ? "warning" : state.actual_enabled ? "success" : ""
  };
});

const exposure = computed(() => {
  const meta = latest.value?.metadata || {};
  const period = latest.value?.period === "day" ? "day" : "night";
  const ms = meta.actual_exposure_ms ?? settings.value?.[`${period}_exposure_ms`];
  const gain = meta.actual_analogue_gain ?? settings.value?.[`${period}_gain`];

  if (!ms) return { value: "-", detail: "No exposure reported" };

  const time = ms >= 1000 ? `${(ms / 1000).toFixed(2).replace(/\.?0+$/, "")}s` : `${Math.round(ms)}ms`;
  return { value: time, detail: gain ? `gain ${Number(gain).toFixed(1)}` : "" };
});
</script>

<template>
  <div class="stack-lg">
    <div v-if="!selectedNode && !loading">
      <div class="panel">
        <EmptyState
          icon="⬡"
          title="No node selected"
          message="Start a node with `python skyhub.py node` and it will appear here automatically."
        />
      </div>
    </div>

    <div v-else class="monitor-grid">
      <section class="panel">
        <div class="panel-header">
          <h2>
            Latest capture
            <span v-if="latest" class="badge" :class="latest.period === 'night' ? 'accent' : ''">
              {{ latest.period }}
            </span>
          </h2>
          <div class="row">
            <button
              v-if="!capturing"
              type="button"
              class="primary"
              :disabled="!selectedNode || busy.capture"
              @click="startCapture"
            >
              Start capture
            </button>
            <button
              v-else
              type="button"
              class="danger"
              :disabled="!selectedNode || busy.capture"
              @click="stopCapture"
            >
              Stop capture
            </button>
          </div>
        </div>

        <div class="panel-body">
          <div class="image-frame">
            <img v-if="latestImageUrl" :src="latestImageUrl" alt="Latest capture" />
            <div v-else-if="loading" class="skeleton" style="width: 100%; height: 340px" />
            <EmptyState
              v-else
              icon="◉"
              title="No captures yet"
              message="The first uploaded frame appears here as soon as a sequence runs."
            />
          </div>

          <div v-if="latest" class="capture-meta">
            <span>Taken <strong>{{ formatDateTime(latest.captured_at) }}</strong></span>
            <span>Size <strong>{{ latest.width }}×{{ latest.height }}</strong></span>
            <span>File <strong>{{ formatBytes(latest.size_bytes) }}</strong></span>
            <span class="truncate">Name <strong>{{ latest.filename }}</strong></span>
          </div>
        </div>
      </section>

      <aside class="stack">
        <div class="monitor-tiles">
          <StatTile
            label="Capture"
            :value="capturing ? 'Running' : 'Idle'"
            :tone="capturing ? 'accent' : ''"
            :detail="settings?.interval_seconds ? `every ${settings.interval_seconds}s` : ''"
            icon="◉"
          />
          <StatTile
            label="Exposure"
            :value="exposure.value"
            :detail="exposure.detail"
            icon="◐"
          />
          <StatTile
            label="Nodes"
            :value="`${onlineCount}/${nodes.length}`"
            :tone="onlineCount ? 'success' : 'danger'"
            :detail="selectedNode?.node_id || ''"
            icon="⬡"
          />
          <StatTile
            label="Resolution"
            :value="settings?.full_resolution ? 'Full sensor' : `${settings?.width || '-'}×${settings?.height || '-'}`"
            :detail="settings?.full_resolution ? 'native readout' : 'pinned size'"
            icon="▦"
          />
        </div>

        <StatTile
          label="Environment"
          :value="environment.value"
          :detail="environment.detail"
          :tone="dewRisk !== null && dewRisk < 2 ? 'warning' : ''"
          icon="☁"
        />

        <StatTile
          label="Heater"
          :value="heater.value"
          :detail="heater.detail"
          :tone="heater.tone"
          icon="♨"
        >
          <template #action>
            <button
              type="button"
              class="sm"
              :class="{ primary: !heaterState?.desired_enabled }"
              :disabled="!selectedNode || busy.heater"
              @click="setHeaterEnabled(!heaterState?.desired_enabled)"
            >
              {{ heaterState?.desired_enabled ? "Turn off" : "Turn on" }}
            </button>
          </template>
        </StatTile>

        <StatTile
          label="Storage"
          :value="formatBytes(storageStats?.capture_storage_bytes)"
          :detail="storageStats ? `${formatBytes(storageStats.disk_free_bytes)} free on disk` : ''"
          icon="▤"
        />
      </aside>
    </div>
  </div>
</template>
