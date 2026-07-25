<script setup>
import { computed, ref, watch } from "vue";
import EmptyState from "../components/ui/EmptyState.vue";
import Lightbox from "../components/Lightbox.vue";
import { captureUrl, formatBytes } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";

const { captures, captureDates, loadCaptures, loadCaptureDates } = useSkyHub();

const selectedCapture = ref(null);
const selectedDate = ref(null);
const selectedPeriod = ref("night");
const loadingCaptures = ref(false);

const selectedGroup = computed(() => (
  captureDates.value.find((group) => group.archive_date === selectedDate.value) || null
));

// The server returns exactly the selected date and period, newest first.
const visibleCaptures = computed(() => captures.value);

const periodCounts = computed(() => ({
  night: selectedGroup.value?.night || 0,
  day: selectedGroup.value?.day || 0
}));

const totalBytes = computed(() => visibleCaptures.value.reduce(
  (sum, capture) => sum + (capture.size_bytes || 0), 0
));

const selectedIndex = computed(() => (
  selectedCapture.value
    ? visibleCaptures.value.findIndex((capture) => capture.path === selectedCapture.value.path)
    : -1
));

const hasPrevious = computed(() => selectedIndex.value > 0);
const hasNext = computed(() => (
  selectedIndex.value >= 0 && selectedIndex.value < visibleCaptures.value.length - 1
));

function formatArchiveDate(archiveDate) {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric"
  }).format(new Date(`${archiveDate}T12:00:00`));
}

function captureTime(capture) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(capture.captured_at || capture.modified_at));
}

async function fetchBucket() {
  if (!selectedDate.value) return;

  loadingCaptures.value = true;

  try {
    await loadCaptures({ archiveDate: selectedDate.value, period: selectedPeriod.value });
  } finally {
    loadingCaptures.value = false;
  }
}

async function refresh() {
  await loadCaptureDates();
  await fetchBucket();
}

watch(captureDates, (groups) => {
  if (!groups.length) {
    selectedDate.value = null;
    return;
  }

  if (!selectedDate.value || !groups.some((group) => group.archive_date === selectedDate.value)) {
    selectedDate.value = groups[0].archive_date;
  }
}, { immediate: true });

watch(selectedGroup, (group) => {
  if (group && !group[selectedPeriod.value]) {
    selectedPeriod.value = group.night ? "night" : "day";
  }
}, { immediate: true });

watch([selectedDate, selectedPeriod], () => {
  selectedCapture.value = null;
  fetchBucket().catch(() => {});
}, { immediate: true });

loadCaptureDates().catch(() => {});
</script>

<template>
  <div class="stack-lg">
    <div class="page-head">
      <div class="page-head-text">
        <h1>Captures</h1>
        <p>
          {{ captureDates.length }} night{{ captureDates.length === 1 ? "" : "s" }} archived ·
          {{ captureDates.reduce((sum, group) => sum + group.total, 0) }} frames
        </p>
      </div>
      <div class="page-head-actions">
        <button type="button" @click="refresh">Refresh</button>
      </div>
    </div>

    <div v-if="!captureDates.length" class="panel">
      <EmptyState
        icon="▦"
        title="Nothing captured yet"
        message="Start a capture sequence from Monitor and frames will collect here, grouped by night."
      />
    </div>

    <div v-else class="capture-browser">
      <section class="panel">
        <div class="panel-header">
          <h2>Dates</h2>
        </div>
        <div class="date-list">
          <button
            v-for="group in captureDates"
            :key="group.archive_date"
            type="button"
            class="date-item"
            :class="{ active: selectedDate === group.archive_date }"
            @click="selectedDate = group.archive_date"
          >
            <strong>{{ formatArchiveDate(group.archive_date) }}</strong>
            <small>{{ group.night }} night · {{ group.day }} day</small>
          </button>
        </div>
      </section>

      <section class="panel">
        <div class="capture-toolbar">
          <div class="segmented">
            <button
              type="button"
              :class="{ active: selectedPeriod === 'night' }"
              :disabled="!periodCounts.night"
              @click="selectedPeriod = 'night'"
            >
              Night {{ periodCounts.night }}
            </button>
            <button
              type="button"
              :class="{ active: selectedPeriod === 'day' }"
              :disabled="!periodCounts.day"
              @click="selectedPeriod = 'day'"
            >
              Day {{ periodCounts.day }}
            </button>
          </div>
          <span class="muted numeric">
            {{ loadingCaptures ? "Loading…" : `${visibleCaptures.length} frames · ${formatBytes(totalBytes)}` }}
          </span>
        </div>

        <div v-if="loadingCaptures" class="capture-grid">
          <div v-for="index in 8" :key="index" class="skeleton" style="aspect-ratio: 4/3" />
        </div>

        <div v-else-if="visibleCaptures.length" class="capture-grid">
          <button
            v-for="capture in visibleCaptures"
            :key="capture.path"
            type="button"
            class="capture-tile"
            @click="selectedCapture = capture"
          >
            <img :src="captureUrl(capture, { thumb: true })" alt="" loading="lazy" />
            <span class="capture-tile-meta">
              <strong>{{ captureTime(capture) }}</strong>
              <small>{{ capture.width }}×{{ capture.height }} · {{ formatBytes(capture.size_bytes) }}</small>
            </span>
          </button>
        </div>

        <EmptyState
          v-else
          icon="○"
          :title="`No ${selectedPeriod} frames on this date`"
          message="Try the other period, or pick a different night."
        />
      </section>
    </div>

    <Lightbox
      :capture="selectedCapture"
      :has-previous="hasPrevious"
      :has-next="hasNext"
      @close="selectedCapture = null"
      @previous="selectedCapture = visibleCaptures[selectedIndex - 1]"
      @next="selectedCapture = visibleCaptures[selectedIndex + 1]"
    />
  </div>
</template>
