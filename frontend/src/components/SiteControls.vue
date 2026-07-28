<script setup>
/* Where the camera is.
 *
 * This used to be two environment variables, which meant that unless someone
 * set them before the first start, every sun calculation in the system ran for
 * a default nobody chose - and the startrail, which now stacks only between
 * astronomical dusk and dawn, would have gated on the wrong sky.
 *
 * The numbers are the setting; the map is an assist for finding them. That
 * order matters: the map needs tiles from the internet, and a server in a shed
 * on a LAN may not have any. Leaflet is loaded on demand and every failure ends
 * with the fields still working. */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useSkyHub } from "../composables/useSkyHub";

const { busy, saveSiteSettings, siteSettings, sunTimes } = useSkyHub();

const baseline = ref(null);
const snapshot = (value) => (value ? JSON.stringify(value) : null);

watch(siteSettings, (value) => { baseline.value = snapshot(value); }, { immediate: true });

const dirty = computed(
  () => Boolean(siteSettings.value) && snapshot(siteSettings.value) !== baseline.value
);

/* Tonight, in one line. Coordinates are four decimal places of abstraction —
 * what an operator actually wants to know is whether there will be any
 * astronomical dark to stack a startrail in. */
const clockFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const asClock = (value) => (value ? clockFormat.format(new Date(value)) : "—");

const darkSummary = computed(() => {
  const sun = sunTimes.value;

  if (!sun) return null;

  if (!sun.dark_from) {
    return {
      tone: "warning",
      text: "The sun never gets 18° below the horizon here tonight, so there is no"
        + " astronomical dark. A startrail set to stack below that depression will"
        + " skip the whole night."
    };
  }

  const hours = Math.floor(sun.dark_hours);
  const minutes = Math.round((sun.dark_hours - hours) * 60);
  const length = hours ? `${hours}h ${minutes}m` : `${minutes}m`;

  return {
    tone: "",
    text: `Astronomical dark tonight runs ${asClock(sun.dark_from)} to`
      + ` ${asClock(sun.dark_until)} — ${length}. That is the window a startrail stacks in.`
  };
});

/* ---- the map ---- */

const mapHost = ref(null);
const mapFailed = ref(false);

let map = null;
let marker = null;
let leaflet = null;
// Set while the map itself is moving the pin, so the watcher below does not
// answer by moving the map, which reads as the pin fighting the cursor.
let movingFromMap = false;

function pinTo(latitude, longitude) {
  if (!siteSettings.value) return;

  movingFromMap = true;
  siteSettings.value.latitude = Number(latitude.toFixed(5));
  siteSettings.value.longitude = Number(longitude.toFixed(5));
  movingFromMap = false;
}

async function buildMap() {
  if (!mapHost.value || !siteSettings.value) return;

  try {
    leaflet = (await import("leaflet")).default;
    await import("leaflet/dist/leaflet.css");
  } catch {
    // No bundle, no map. The fields are the setting and they are untouched.
    mapFailed.value = true;
    return;
  }

  const start = [siteSettings.value.latitude, siteSettings.value.longitude];

  map = leaflet.map(mapHost.value, { attributionControl: true }).setView(start, 9);

  leaflet
    .tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: "&copy; OpenStreetMap contributors"
    })
    .addTo(map);

  /* A div icon rather than Leaflet's own marker: the default one is a PNG
   * referenced from the stylesheet by a relative path, which a bundler rewrites
   * and then cannot find. A styled element has no asset to lose. */
  marker = leaflet
    .marker(start, {
      draggable: true,
      icon: leaflet.divIcon({ className: "site-pin", iconSize: [18, 18] })
    })
    .addTo(map);

  marker.on("drag", (event) => {
    const { lat, lng } = event.target.getLatLng();
    pinTo(lat, lng);
  });

  map.on("click", (event) => {
    marker.setLatLng(event.latlng);
    pinTo(event.latlng.lat, event.latlng.lng);
  });
}

/* Typing coordinates moves the pin, so the two are never showing different
 * places. Guarded against the values the map itself just wrote. */
watch(
  () => [siteSettings.value?.latitude, siteSettings.value?.longitude],
  ([latitude, longitude]) => {
    if (!marker || movingFromMap) return;
    if (!Number.isFinite(Number(latitude)) || !Number.isFinite(Number(longitude))) return;

    const position = [Number(latitude), Number(longitude)];

    marker.setLatLng(position);
    map.panTo(position, { animate: false });
  }
);

// The map cannot lay out until it has a size, so it waits for the settings that
// give it a starting position rather than building against a null.
watch(siteSettings, (value) => {
  if (value && !map && !mapFailed.value) buildMap();
});

onMounted(() => {
  if (siteSettings.value) buildMap();
});

onBeforeUnmount(() => {
  if (map) {
    map.remove();
    map = null;
  }
});

function useBrowserLocation() {
  if (!navigator.geolocation) return;

  navigator.geolocation.getCurrentPosition(
    (position) => pinTo(position.coords.latitude, position.coords.longitude),
    () => {},
    { enableHighAccuracy: true, timeout: 10000 }
  );
}
</script>

<template>
  <section v-if="siteSettings" class="panel">
    <div class="panel-header">
      <h2>
        Location
        <span v-if="dirty" class="badge warning">unsaved</span>
      </h2>
    </div>

    <div class="panel-body">
      <p class="field-hint">
        Where the camera is. Everything the server works out about the sun comes from
        this — the elevation printed on each frame, the sunrise and sunset that split
        the archive into day and night, and the astronomical dusk-to-dawn window the
        startrail stacks in.
      </p>

      <div class="field-grid">
        <label class="field">
          <span>Latitude <em class="field-unit">°N</em></span>
          <input v-model.number="siteSettings.latitude" type="number" step="0.00001" min="-90" max="90" />
        </label>
        <label class="field">
          <span>Longitude <em class="field-unit">°E</em></span>
          <input v-model.number="siteSettings.longitude" type="number" step="0.00001" min="-180" max="180" />
        </label>
        <label class="field">
          <span>Elevation <em class="field-unit">m</em></span>
          <input v-model.number="siteSettings.elevation_m" type="number" step="1" />
        </label>
        <label class="field">
          <span>Timezone</span>
          <input v-model="siteSettings.timezone" placeholder="Europe/Berlin" />
        </label>
        <label class="field">
          <span>Name <em class="field-unit">optional</em></span>
          <input v-model="siteSettings.label" placeholder="Garden" maxlength="120" />
        </label>
      </div>

      <div class="section">
        <div class="row-between">
          <div class="section-title">Drop a pin</div>
          <button type="button" class="ghost" @click="useBrowserLocation">Use my location</button>
        </div>

        <div v-if="mapFailed" class="callout">
          The map could not load, which usually means this browser has no route to the
          internet. Type the coordinates instead — they are what the server uses either way.
        </div>
        <div v-else ref="mapHost" class="site-map" />

        <p class="field-hint">
          Click the map or drag the pin. A few hundred metres either way changes nothing
          the sun does; a wrong country changes all of it.
        </p>
      </div>

      <p v-if="darkSummary" class="callout" :class="darkSummary.tone">
        {{ darkSummary.text }}
      </p>

      <div v-if="sunTimes" class="storage-grid">
        <div class="storage-item">
          <span>Sunset</span>
          <strong>{{ asClock(sunTimes.sunset) }}</strong>
        </div>
        <div class="storage-item">
          <span>Astronomical dusk</span>
          <strong>{{ asClock(sunTimes.dark_from) }}</strong>
        </div>
        <div class="storage-item">
          <span>Astronomical dawn</span>
          <strong>{{ asClock(sunTimes.dark_until) }}</strong>
        </div>
        <div class="storage-item">
          <span>Sunrise</span>
          <strong>{{ asClock(sunTimes.sunrise) }}</strong>
        </div>
      </div>
    </div>

    <div class="panel-footer">
      <span class="muted grow">
        Saved changes apply to the next capture — nothing needs restarting.
      </span>
      <button type="button" class="primary" :disabled="!dirty || busy.site" @click="saveSiteSettings">
        {{ busy.site ? "Saving…" : "Save location" }}
      </button>
    </div>
  </section>
</template>

<style>
.site-map {
  height: 260px;
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  /* Leaflet puts its panes above everything by default, which would float the
   * map over the sticky topbar while scrolling. */
  z-index: 0;
}

.site-map .leaflet-container {
  background: var(--surface-inset);
  font: inherit;
}

/* The tiles are drawn for a light page. Rather than ship a second tile source,
 * the dark theme inverts them - the result reads as a night map and, more to the
 * point, stops the panel being a white rectangle in a dark UI. */
:root[data-theme="dark"] .site-map .leaflet-tile-pane {
  filter: invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.9);
}

.site-pin {
  border: 2px solid var(--accent);
  border-radius: 50%;
  background: var(--accent-soft);
  box-shadow: 0 0 0 2px var(--bg), 0 0 12px var(--accent-ring);
  cursor: grab;
}

.site-pin:active {
  cursor: grabbing;
}
</style>
