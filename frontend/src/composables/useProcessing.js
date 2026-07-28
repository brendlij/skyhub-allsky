import { computed, ref } from "vue";
import { requestJson } from "../api/skyhub";
import { onProcessingEvent, useSkyHub } from "./useSkyHub";
import { useToasts } from "./useToasts";

/* Derived products, and the processors that make them.
 *
 * Live products are rewritten on the server every capture at the same URL, so
 * "refresh" here means busting the browser's cache rather than fetching a new
 * path - hence `liveVersion`, bumped whenever the pipeline says it wrote
 * something. The server sends `Cache-Control: no-cache` with an ETag, so the
 * reload is usually a 304 and costs nothing.
 */

const { notifyError } = useToasts();

const products = ref([]);
const dates = ref([]);
const sessions = ref([]);
const status = ref(null);
const selectedDate = ref(null);
const loading = ref(false);
const liveVersion = ref(Date.now());
const progress = ref({});

let subscribed = false;

const KIND_LABELS = {
  startrail_live: "Live startrail",
  startrail: "Startrail",
  startrail_build: "Startrail build",
  keogram_live: "Live keogram",
  keogram: "Keogram",
  timelapse: "Timelapse"
};

const KIND_ORDER = [
  "startrail_live",
  "keogram_live",
  "startrail",
  "keogram",
  "timelapse",
  "startrail_build"
];

export function productLabel(kind) {
  return KIND_LABELS[kind] || kind;
}

export function isVideo(product) {
  return String(product.media_type || "").startsWith("video/");
}

function bust(url, product) {
  // Live products are rewritten at the same path on every capture, so the cache
  // key has to move even though the URL does not. Final ones never change, so
  // leaving them alone keeps them cacheable.
  return product.state === "live" ? `${url}?v=${liveVersion.value}` : url;
}

/** The full-size original. Use it for downloads and open-in-new-tab, not <img>. */
export function productUrl(product) {
  return bust(product.url, product);
}

/* What to actually put in an <img>.
 *
 * The manager derives a web-sized copy of anything large, so a gallery never
 * pulls a 4056px startrail down a phone connection to display it at 400px. Null
 * means the original was already small enough, so it is the right file to use. */
export function productDisplayUrl(product) {
  return bust(product.web_url || product.url, product);
}

/** The smallest variant, for tiles and lists. */
export function productPreviewUrl(product) {
  return bust(product.preview_url || product.web_url || product.url, product);
}

export function useProcessing() {
  const { selectedNodeId } = useSkyHub();

  if (!subscribed) {
    subscribed = true;

    onProcessingEvent((event) => {
      if (selectedNodeId.value && event.node_id !== selectedNodeId.value) return;

      if (event.type === "processing.products") {
        liveVersion.value = Date.now();

        // A product appearing for the first time needs the list, not just a new
        // cache key. Refreshing on every frame would be wasteful, so only a kind
        // that is not already listed triggers one.
        const known = new Set(products.value.map((product) => product.kind));
        const unseen = (event.products || []).some((product) => !known.has(product.kind));

        if (unseen) loadProducts().catch(() => {});

        return;
      }

      if (event.type === "processing.progress") {
        // Keyed by session then processor, mirroring the server. Replaced rather
        // than mutated so the computed views downstream actually re-evaluate.
        const key = `${event.archive_date}/${event.period}`;

        progress.value = {
          ...progress.value,
          [key]: {
            ...(progress.value[key] || {}),
            [event.processor]: {
              stage: event.stage,
              percent: event.percent,
              detail: event.detail
            }
          }
        };

        return;
      }

      if (event.type === "processing.session" && event.status === "closed") {
        loadProducts().catch(() => {});
        loadSessions().catch(() => {});
      }
    });
  }

  const live = computed(() => products.value.filter((product) => product.state === "live"));
  const finished = computed(() => products.value.filter((product) => product.state !== "live"));

  const byPeriod = computed(() => {
    const groups = { night: [], day: [] };

    for (const product of finished.value) {
      (groups[product.period] ||= []).push(product);
    }

    for (const list of Object.values(groups)) {
      list.sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind));
    }

    return groups;
  });

  async function loadProducts() {
    const params = new URLSearchParams();

    if (selectedNodeId.value) params.set("node_id", selectedNodeId.value);
    if (selectedDate.value) params.set("archive_date", selectedDate.value);

    const result = await requestJson(`/api/processing/products?${params}`);
    products.value = result.products || [];
  }

  async function loadDates() {
    const params = new URLSearchParams();

    if (selectedNodeId.value) params.set("node_id", selectedNodeId.value);

    const result = await requestJson(`/api/processing/products/dates?${params}`);
    dates.value = result.dates || [];

    // Default to the newest night that has anything, so the view opens on
    // something rather than on an empty date picker.
    if (!selectedDate.value && dates.value.length) selectedDate.value = dates.value[0];
  }

  async function loadSessions() {
    const params = new URLSearchParams();

    if (selectedNodeId.value) params.set("node_id", selectedNodeId.value);

    const result = await requestJson(`/api/processing/sessions?${params}`);
    sessions.value = result.sessions || [];
  }

  async function loadStatus() {
    status.value = await requestJson("/api/processing/status");
  }

  async function refresh() {
    loading.value = true;

    try {
      await loadDates();
      await Promise.all([loadProducts(), loadSessions(), loadStatus()]);
    } catch (error) {
      notifyError(error);
    } finally {
      loading.value = false;
    }
  }

  async function selectDate(date) {
    selectedDate.value = date;
    loading.value = true;

    try {
      await loadProducts();
    } catch (error) {
      notifyError(error);
    } finally {
      loading.value = false;
    }
  }

  async function updateProcessor(name, values) {
    await requestJson(`/api/processing/processors/${name}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values)
    });

    await loadStatus();
  }

  async function closeSession(session) {
    const result = await requestJson("/api/processing/sessions/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        node_id: session.node_id,
        archive_date: session.archive_date,
        period: session.period
      })
    });

    await refresh();

    return result;
  }

  /** Collect into a fresh session from now, whatever the sun is doing. */
  async function startRun(label) {
    const result = await requestJson("/api/processing/runs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: selectedNodeId.value, label: label || null })
    });

    await refresh();

    return result;
  }

  /** Live progress for one session, from the WebSocket or the last API load. */
  function progressFor(session) {
    return progress.value[`${session.archive_date}/${session.period}`] || session.progress || {};
  }

  return {
    products,
    dates,
    sessions,
    status,
    progress,
    progressFor,
    selectedDate,
    loading,
    live,
    finished,
    byPeriod,
    liveVersion,
    closeSession,
    loadStatus,
    refresh,
    selectDate,
    startRun,
    updateProcessor
  };
}
