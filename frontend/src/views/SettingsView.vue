<script setup>
import { computed, ref, watch } from "vue";
import AccessControls from "../components/AccessControls.vue";
import ColourControls from "../components/ColourControls.vue";
import ProcessingControls from "../components/ProcessingControls.vue";
import SecurityControls from "../components/SecurityControls.vue";
import SiteControls from "../components/SiteControls.vue";
import MaskControls from "../components/MaskControls.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import { formatBytes } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";

const {
  busy,
  deviceSettings,
  latest,
  saveDeviceSettings,
  saveSettings,
  saveStorageSettings,
  settings,
  storageSettings,
  storageStats
} = useSkyHub();

const PERIODS = [
  { id: "night", label: "Night", icon: "🌙" },
  { id: "day", label: "Day", icon: "☀️" }
];

/* The node reports the period of its last frame, which is the profile actually
 * in use - so the tab opens on the settings that are live rather than always on
 * night. */
const livePeriod = computed(() => {
  const period = latest.value?.period;
  return period === "day" || period === "night" ? period : null;
});

const activePeriod = ref(livePeriod.value || "night");
const periodPinned = ref(false);

function selectPeriod(period) {
  periodPinned.value = true;
  activePeriod.value = period;
}

/* Dirty tracking: the old form had a Save button with no indication of unsaved
 * work, so navigating away silently discarded edits. */
const baseline = ref(null);
const storageBaseline = ref(null);
const deviceBaseline = ref(null);

const snapshot = (value) => (value ? JSON.stringify(value) : null);

watch(settings, (value) => { baseline.value = snapshot(value); }, { immediate: true, deep: false });
watch(storageSettings, (value) => { storageBaseline.value = snapshot(value); }, { immediate: true, deep: false });
watch(deviceSettings, (value) => { deviceBaseline.value = snapshot(value); }, { immediate: true, deep: false });

const cameraDirty = computed(() => Boolean(settings.value) && snapshot(settings.value) !== baseline.value);
const storageDirty = computed(() => Boolean(storageSettings.value) && snapshot(storageSettings.value) !== storageBaseline.value);
const deviceDirty = computed(() => Boolean(deviceSettings.value) && snapshot(deviceSettings.value) !== deviceBaseline.value);

/* Follow the node until the tab is picked by hand. Unsaved work pins it too, so
 * a sunset can never move the form out from under an edit in flight. */
watch(livePeriod, (period) => {
  if (period && !periodPinned.value && !cameraDirty.value) activePeriod.value = period;
});

// Every frame off the sensor costs a full exposure. A fixed exposure needs only
// the frame it keeps, but auto re-tunes constantly and pays for settle frames on
// top - so a long exposure quietly overrides the interval either way.
const AUTO_FRAMES_PER_CAPTURE = 4;

// Exposures are entered in milliseconds because that is what the node takes, but
// a night exposure is thought about in seconds.
function asSeconds(milliseconds) {
  const value = Number(milliseconds);

  if (!value || value < 1000) return "";

  return `${Number((value / 1000).toFixed(1))}s`;
}

// Saved settings reach a running sequence on its next frame, so the honest answer
// to "when does this take effect" depends on how long the frame in flight is.
const applyHint = computed(() => {
  if (!settings.value?.capture_enabled) return "Applies when capture next starts.";

  const period = activePeriod.value;
  const exposureMs = Number(
    settings.value[`${period}_auto_exposure`]
      ? settings.value[`${period}_max_exposure_ms`]
      : settings.value[`${period}_exposure_ms`]
  );

  if (exposureMs >= 30000) {
    return `Capture is running: saved changes apply to the next capture, which can be`
      + ` up to ${Math.round(exposureMs / 1000)}s away while the current exposure finishes.`
      + " Restart capture to apply immediately.";
  }

  return "Capture is running: saved changes apply to the next capture, no restart needed.";
});

const cadenceWarning = computed(() => {
  if (!settings.value) return null;

  const period = activePeriod.value;
  const auto = Boolean(settings.value[`${period}_auto_exposure`]);
  const intervalSeconds = Number(
    settings.value[`${period}_interval_seconds`] || settings.value.interval_seconds
  );

  if (auto && !Number(settings.value[`${period}_max_exposure_ms`])) {
    return "With no maximum, auto exposure can run all the way to the sensor's own"
      + " ceiling on a dark night, and a single capture then takes many minutes.";
  }

  const exposureMs = Number(
    auto
      ? settings.value[`${period}_max_exposure_ms`]
      : settings.value[`${period}_exposure_ms`]
  );

  if (!exposureMs || !intervalSeconds) return null;

  const captureSeconds = (exposureMs / 1000) * (auto ? AUTO_FRAMES_PER_CAPTURE : 1);

  if (captureSeconds <= intervalSeconds) return null;

  return `${auto ? "At the maximum exposure a" : "A"} capture takes about`
    + ` ${Math.round(captureSeconds)}s, longer than the ${intervalSeconds}s interval,`
    + " so frames will be spaced by the exposure rather than by the interval.";
});

const storageUsedRatio = computed(() => {
  const max = storageSettings.value?.max_storage_gb;
  const used = storageStats.value?.capture_storage_bytes;

  if (!max || !used) return null;
  return Math.min(1, used / (max * 1024 ** 3));
});

const meterTone = computed(() => {
  const ratio = storageUsedRatio.value;
  if (ratio === null) return "";
  if (ratio > 0.9) return "danger";
  if (ratio > 0.7) return "warning";
  return "";
});

const STORAGE_KEYS = [
  ["capture_storage_bytes", "Capture total"],
  ["captures_bytes", "Rendered"],
  ["originals_bytes", "Originals"],
  ["thumbnails_bytes", "Thumbnails"],
  ["database_bytes", "Database"],
  ["disk_free_bytes", "Disk free"]
];
</script>

<template>
  <div class="stack-lg">
    <div class="page-head">
      <div class="page-head-text">
        <h1>Settings</h1>
        <p>Camera, colour, hardware and storage for the selected node — plus this server's location, your account and its access</p>
      </div>
    </div>

    <div v-if="!settings" class="panel">
      <EmptyState icon="⚙" title="No node selected" message="Pick a node in the top bar to edit its settings." />
    </div>

    <template v-else>
      <section class="panel">
        <div class="panel-header">
          <h2>
            Camera
            <span v-if="cameraDirty" class="badge warning">unsaved</span>
          </h2>
        </div>

        <div class="panel-body">
          <div class="field-grid">
            <label class="field">
              <span>Day interval <em class="field-unit">seconds</em></span>
              <input v-model.number="settings.day_interval_seconds" type="number" min="1" />
            </label>
            <label class="field">
              <span>Night interval <em class="field-unit">seconds</em></span>
              <input v-model.number="settings.night_interval_seconds" type="number" min="1" />
            </label>
            <label class="field">
              <span>Format</span>
              <select v-model="settings.format">
                <option value="jpg">jpg</option>
                <option value="png">png</option>
              </select>
            </label>
            <p class="field-hint">
              The current active interval is {{ activePeriod === 'day' ? settings.day_interval_seconds : settings.night_interval_seconds }}s.
            </p>
          </div>

          <div class="section">
            <div class="section-title">Resolution</div>
            <label class="check">
              <input v-model="settings.full_resolution" type="checkbox" />
              Full sensor resolution
            </label>
            <p class="field-hint">
              {{ settings.full_resolution
                ? "Captures at the sensor's native size — full field of view, largest files."
                : "Captures at the size below. The node raises the height if the ratio would crop the sensor." }}
            </p>
            <div class="field-grid">
              <label class="field">
                <span>Width</span>
                <input v-model.number="settings.width" type="number" min="1" :disabled="settings.full_resolution" />
              </label>
              <label class="field">
                <span>Height</span>
                <input v-model.number="settings.height" type="number" min="1" :disabled="settings.full_resolution" />
              </label>
            </div>
          </div>

          <div class="section">
            <div class="section-title">Exposure &amp; colour</div>
            <div class="period-switch" role="tablist" aria-label="Exposure and colour period">
              <button
                v-for="option in PERIODS"
                :key="option.id"
                type="button"
                role="tab"
                :aria-selected="activePeriod === option.id"
                class="period-tab"
                :class="{ active: activePeriod === option.id }"
                @click="selectPeriod(option.id)"
              >
                <span class="period-tab-icon" aria-hidden="true">{{ option.icon }}</span>
                <span class="period-tab-label">{{ option.label }}</span>
                <span v-if="livePeriod === option.id" class="period-tab-now">now</span>
              </button>
            </div>

            <p class="field-hint">
              <template v-if="!livePeriod">
                Editing the {{ activePeriod }} profile.
              </template>
              <template v-else-if="livePeriod === activePeriod">
                Editing the {{ activePeriod }} profile — the one the node is using right now.
              </template>
              <template v-else>
                The node is on {{ livePeriod }} right now, so these {{ activePeriod }} values
                take effect at the next {{ activePeriod === 'day' ? 'sunrise' : 'sunset' }}.
              </template>
            </p>

            <div class="period-card">
              <div class="field-grid">
                <label class="check">
                  <input v-model="settings[`${activePeriod}_auto_exposure`]" type="checkbox" />
                  Auto exposure
                </label>
                <label class="field">
                  <span>
                    Exposure <em class="field-unit">ms</em>
                    <em class="field-value">{{ asSeconds(settings[`${activePeriod}_exposure_ms`]) }}</em>
                  </span>
                  <input
                    v-model.number="settings[`${activePeriod}_exposure_ms`]"
                    type="number"
                    min="1"
                    :disabled="settings[`${activePeriod}_auto_exposure`]"
                  />
                </label>
                <label class="field">
                  <span>
                    Max exposure <em class="field-unit">ms</em>
                    <em class="field-value">{{ asSeconds(settings[`${activePeriod}_max_exposure_ms`]) }}</em>
                  </span>
                  <input
                    v-model.number="settings[`${activePeriod}_max_exposure_ms`]"
                    type="number"
                    min="1"
                    :disabled="!settings[`${activePeriod}_auto_exposure`]"
                  />
                </label>
                <label class="check">
                  <input v-model="settings[`${activePeriod}_auto_gain`]" type="checkbox" />
                  Auto gain
                </label>
                <label class="field">
                  <span>Gain</span>
                  <input
                    v-model.number="settings[`${activePeriod}_gain`]"
                    type="number"
                    step="0.1"
                    min="0"
                    :disabled="settings[`${activePeriod}_auto_gain`]"
                  />
                </label>
                <label class="field">
                  <span>Max gain</span>
                  <input
                    v-model.number="settings[`${activePeriod}_max_gain`]"
                    type="number"
                    step="0.1"
                    min="1"
                    :disabled="!settings[`${activePeriod}_auto_gain`]"
                  />
                </label>
              </div>

              <p v-if="cadenceWarning" class="callout warning">
                {{ cadenceWarning }}
              </p>

              <p class="field-hint">
                Exposure and gain are only handed to the auto controller when the box above
                is ticked; otherwise the node uses exactly the values set here. The maximums
                bound the controller and are ignored in manual mode.
              </p>

              <ColourControls :settings="settings" :period="activePeriod" />
            </div>
          </div>
        </div>

        <div class="panel-footer">
          <span class="muted grow">
            <template v-if="cameraDirty">Unsaved changes · </template>{{ applyHint }}
          </span>
          <button type="button" class="primary" :disabled="!cameraDirty || busy.settings" @click="saveSettings">
            {{ busy.settings ? "Saving…" : "Save camera settings" }}
          </button>
        </div>
      </section>

      <div class="settings-grid">
        <MaskControls />

        <section v-if="deviceSettings" class="panel">
          <div class="panel-header">
            <h2>
              Hardware
              <span v-if="deviceDirty" class="badge warning">unsaved</span>
            </h2>
          </div>
          <div class="panel-body">
            <div class="section">
              <div class="section-title">Heater</div>
              <div class="field-grid">
                <label class="field">
                  <span>Driver</span>
                  <select v-model="deviceSettings.devices.heater.driver">
                    <option value="gpiozero">gpiozero</option>
                    <option value="mock">mock</option>
                    <option value="disabled">disabled</option>
                  </select>
                </label>
                <label class="field">
                  <span>GPIO pin</span>
                  <input v-model.number="deviceSettings.devices.heater.gpio_pin" type="number" min="0" />
                </label>
                <label class="check">
                  <input v-model="deviceSettings.devices.heater.active_high" type="checkbox" />
                  Active high
                </label>
              </div>
            </div>

            <div class="section">
              <div class="section-title">Environment sensor</div>
              <div class="field-grid">
                <label class="field">
                  <span>Driver</span>
                  <select v-model="deviceSettings.devices.environment.driver">
                    <option value="bme280">bme280</option>
                    <option value="mock">mock</option>
                    <option value="disabled">disabled</option>
                  </select>
                </label>
                <label class="field">
                  <span>Interval <em class="field-unit">seconds</em></span>
                  <input v-model.number="deviceSettings.devices.environment.interval_seconds" type="number" min="1" />
                </label>
                <label class="field">
                  <span>I2C bus</span>
                  <input v-model.number="deviceSettings.devices.environment.bme280_i2c_bus" type="number" min="0" />
                </label>
                <label class="field">
                  <span>I2C address</span>
                  <input v-model="deviceSettings.devices.environment.bme280_i2c_address" />
                </label>
              </div>
            </div>
          </div>
          <div class="panel-footer">
            <button type="button" class="primary" :disabled="!deviceDirty || busy.devices" @click="saveDeviceSettings">
              {{ busy.devices ? "Saving…" : "Save hardware" }}
            </button>
          </div>
        </section>

        <section v-if="storageSettings" class="panel">
          <div class="panel-header">
            <h2>
              Storage
              <span v-if="storageDirty" class="badge warning">unsaved</span>
            </h2>
          </div>
          <div class="panel-body">
            <div class="field-grid">
              <label class="check">
                <input v-model="storageSettings.night_capture_enabled" type="checkbox" />
                Keep night captures
              </label>
              <label class="check">
                <input v-model="storageSettings.day_capture_enabled" type="checkbox" />
                Keep day captures
              </label>
              <label class="field">
                <span>Retention <em class="field-unit">days</em></span>
                <input v-model.number="storageSettings.retention_days" type="number" min="1" placeholder="Unlimited" />
              </label>
              <label class="field">
                <span>Max storage <em class="field-unit">GB</em></span>
                <input
                  v-model.number="storageSettings.max_storage_gb"
                  type="number"
                  min="0.1"
                  step="0.1"
                  placeholder="Unlimited"
                />
              </label>
            </div>

            <p
              v-if="!storageSettings.night_capture_enabled || !storageSettings.day_capture_enabled"
              class="callout warning"
            >
              Uploads for a disabled period are discarded by the server and deleted from
              the node — those frames are not kept anywhere.
            </p>

            <div v-if="storageUsedRatio !== null" class="section">
              <div class="row-between">
                <span class="muted">Against the {{ storageSettings.max_storage_gb }} GB cap</span>
                <span class="numeric">{{ Math.round(storageUsedRatio * 100) }}%</span>
              </div>
              <div class="meter">
                <div class="meter-fill" :class="meterTone" :style="{ width: `${storageUsedRatio * 100}%` }" />
              </div>
            </div>

            <div v-if="storageStats" class="storage-grid">
              <div v-for="[storageKey, label] in STORAGE_KEYS" :key="storageKey" class="storage-item">
                <span>{{ label }}</span>
                <strong>{{ formatBytes(storageStats[storageKey]) }}</strong>
              </div>
            </div>
          </div>
          <div class="panel-footer">
            <button type="button" class="primary" :disabled="!storageDirty || busy.storage" @click="saveStorageSettings">
              {{ busy.storage ? "Saving…" : "Save storage policy" }}
            </button>
          </div>
        </section>
      </div>
    </template>

    <!-- Outside the node branch: these are about the server and the account, not
         about whichever camera happens to be selected, and they have to stay
         reachable when there are no nodes at all. -->
    <div class="settings-grid">
      <SiteControls />
      <ProcessingControls />
      <SecurityControls />
      <AccessControls />
    </div>
  </div>
</template>
