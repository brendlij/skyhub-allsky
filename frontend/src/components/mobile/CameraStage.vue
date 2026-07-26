<script setup>
import { computed, ref } from "vue";
import FloatingActionBar from "./FloatingActionBar.vue";
import ImageViewer from "./ImageViewer.vue";
import { formatDateTime } from "../../api/skyhub";
import { useSkyHub } from "../../composables/useSkyHub";
import { useToasts } from "../../composables/useToasts";

/* The live view, treated as a camera app rather than a dashboard widget:
 * the frame is the page, its telemetry rides on top of it as chips, and the
 * actions float over it instead of sitting in a toolbar somewhere else.
 */

defineProps({
  chips: { type: Array, default: () => [] }
});

const emit = defineEmits(["open-details"]);

const {
  busy,
  latest,
  latestImageUrl,
  loading,
  nodes,
  selectedNodeId,
  selectNode,
  settings,
  startCapture,
  stopCapture
} = useSkyHub();

const { notify } = useToasts();

const viewerOpen = ref(false);
const chipsVisible = ref(true);
const capturing = computed(() => Boolean(settings.value?.capture_enabled));

const actions = computed(() => {
  const list = [];

  if (capturing.value) {
    list.push({ id: "stop", label: "Stop", icon: "■", tone: "danger", primary: true, disabled: busy.capture });
  } else {
    list.push({ id: "start", label: "Start", icon: "●", tone: "accent", primary: true, disabled: busy.capture });
  }

  list.push({ id: "chips", label: chipsVisible.value ? "Hide telemetry" : "Show telemetry", icon: chipsVisible.value ? "◐" : "◑" });

  if (latestImageUrl.value) {
    list.push({ id: "fullscreen", label: "Fullscreen", icon: "⛶" });
  }

  return list;
});

function onAction(id) {
  if (id === "start") return startCapture();
  if (id === "stop") return stopCapture();
  if (id === "chips") { chipsVisible.value = !chipsVisible.value; return; }
  if (id === "fullscreen") { viewerOpen.value = true; return; }
}

// captureUrl already carries the api_key, so this needs no further signing.
const downloadUrl = computed(() => latestImageUrl.value);

async function share() {
  if (!latest.value) return;

  const url = new URL(downloadUrl.value, window.location.origin).toString();

  // Web Share is the native sheet where it exists; a copied link is the honest
  // fallback everywhere else.
  if (navigator.share) {
    try {
      await navigator.share({ title: latest.value.filename, url });
      return;
    } catch {
      return; // Cancelled by the user.
    }
  }

  await navigator.clipboard?.writeText(url);
  notify("Link copied");
}

/* ---------- Gestures ---------- */

const SWIPE_DISTANCE = 60;
const LONG_PRESS_MS = 450;

let gesture = null;
let longPressTimer = null;

function onPointerDown(event) {
  gesture = { x: event.clientX, y: event.clientY, moved: false };

  longPressTimer = window.setTimeout(() => {
    if (gesture && !gesture.moved) {
      gesture.handled = true;
      emit("open-details");
    }
  }, LONG_PRESS_MS);
}

function onPointerMove(event) {
  if (!gesture) return;

  if (Math.abs(event.clientX - gesture.x) > 10 || Math.abs(event.clientY - gesture.y) > 10) {
    gesture.moved = true;
    window.clearTimeout(longPressTimer);
  }
}

function onPointerUp(event) {
  window.clearTimeout(longPressTimer);

  if (!gesture || gesture.handled) {
    gesture = null;
    return;
  }

  const deltaX = event.clientX - gesture.x;
  const deltaY = event.clientY - gesture.y;
  const horizontal = Math.abs(deltaX) > Math.abs(deltaY);

  // A sideways swipe pages through the cameras, the way a phone camera app
  // switches lenses. Only when there is somewhere to go.
  if (horizontal && Math.abs(deltaX) > SWIPE_DISTANCE && nodes.value.length > 1) {
    const index = nodes.value.findIndex((node) => node.node_id === selectedNodeId.value);
    const next = nodes.value[(index + (deltaX < 0 ? 1 : -1) + nodes.value.length) % nodes.value.length];

    if (next) selectNode(next.node_id);
    gesture = null;
    return;
  }

  if (!gesture.moved && latestImageUrl.value) {
    viewerOpen.value = true;
  }

  gesture = null;
}
</script>

<template>
  <div class="camera">
    <div
      class="camera-frame"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
      <img
        v-if="latestImageUrl"
        :src="latestImageUrl"
        alt="Live capture"
        decoding="async"
        draggable="false"
      />
      <div v-else-if="loading" class="skeleton camera-skeleton" />
      <p v-else class="camera-empty">Waiting for the first frame from this node.</p>

      <Transition name="chips">
        <div v-if="chipsVisible && chips.length" class="camera-chips">
          <span v-for="chip in chips" :key="chip.label" class="chip" :class="chip.tone">
            <span v-if="chip.dot" class="dot" :class="{ pulse: chip.pulse }" />
            {{ chip.value }}
          </span>
        </div>
      </Transition>

      <div v-if="nodes.length > 1" class="camera-pager" aria-hidden="true">
        <span
          v-for="node in nodes"
          :key="node.node_id"
          class="camera-pager-dot"
          :class="{ active: node.node_id === selectedNodeId }"
        />
      </div>

      <FloatingActionBar class="camera-actions" :actions="actions" @select="onAction" />
    </div>

    <ImageViewer
      :open="viewerOpen"
      :src="latestImageUrl || ''"
      :title="latest?.filename || 'Live capture'"
      :subtitle="latest ? formatDateTime(latest.captured_at) : ''"
      @close="viewerOpen = false"
    >
      <template #actions>
        <a v-if="downloadUrl" class="viewer-action" :href="downloadUrl" download>Download</a>
        <button type="button" class="viewer-action" @click="share">Share</button>
      </template>
    </ImageViewer>
  </div>
</template>

<style scoped>
.camera-frame {
  position: relative;
  display: grid;
  overflow: hidden;
  place-items: center;
  /* The frame takes the top half of the screen and no more: enough to read the
   * sky at a glance, little enough that the status below it is not a scroll away. */
  height: clamp(240px, 52dvh, 560px);
  border-radius: var(--radius-card);
  background: var(--image-backdrop);
  /* Vertical scroll still belongs to the page; horizontal is ours to page nodes. */
  touch-action: pan-y;
  -webkit-user-select: none;
  user-select: none;
}

.camera-frame img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.camera-skeleton {
  width: 100%;
  height: 100%;
  border-radius: 0;
}

.camera-empty {
  padding: var(--space-6);
  color: var(--overlay-text-muted);
  font-size: 14px;
  text-align: center;
}

.camera-chips {
  position: absolute;
  top: var(--space-3);
  left: var(--space-3);
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  max-width: calc(100% - var(--space-6));
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--overlay-border);
  border-radius: var(--radius-full);
  padding: 4px 10px;
  background: var(--overlay-panel);
  color: var(--overlay-text);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  font-weight: 550;
  backdrop-filter: blur(16px);
}

.chip.success { color: var(--success-400); }
.chip.warning { color: var(--warning-400); }
.chip.danger { color: var(--danger-400); }

.camera-actions {
  position: absolute;
  right: var(--space-3);
  bottom: var(--space-3);
  left: var(--space-3);
}

.camera-pager {
  position: absolute;
  bottom: calc(var(--space-3) + var(--tap-target) + var(--space-2));
  left: 50%;
  display: flex;
  gap: 6px;
  transform: translateX(-50%);
}

.camera-pager-dot {
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: #ffffff59;
  transition: background var(--motion-fast);
}

.camera-pager-dot.active {
  background: #ffffff;
}

.viewer-action {
  display: inline-flex;
  min-height: var(--tap-target);
  align-items: center;
  border: 1px solid #ffffff2e;
  border-radius: var(--radius-full);
  padding: 0 var(--space-5);
  background: #ffffff14;
  color: #ffffff;
  font-size: 14px;
  font-weight: 550;
  text-decoration: none;
}

.viewer-action:hover {
  border-color: #ffffff3d;
  background: #ffffff1f;
  text-decoration: none;
}

.chips-enter-active,
.chips-leave-active {
  transition: opacity var(--motion-fast), transform var(--motion-fast);
}

.chips-enter-from,
.chips-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
