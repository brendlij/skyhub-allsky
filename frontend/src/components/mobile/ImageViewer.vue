<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";

/* Fullscreen image with the gestures a photo viewer is expected to have:
 * pinch to zoom, double tap to zoom, drag to pan while zoomed, and drag down to
 * dismiss while not.
 *
 * Everything is a single transform on one element, so the browser can keep the
 * whole gesture on the compositor - no layout, no reflow, no dropped frames on a
 * 12 megapixel JPEG.
 */

const props = defineProps({
  open: { type: Boolean, default: false },
  src: { type: String, default: "" },
  title: { type: String, default: "" },
  subtitle: { type: String, default: "" }
});

const emit = defineEmits(["close"]);

const MAX_SCALE = 5;
const DOUBLE_TAP_SCALE = 2.5;
const DISMISS_DISTANCE = 120;

const scale = ref(1);
const offset = ref({ x: 0, y: 0 });
const dismissOffset = ref(0);
const animating = ref(true);

// Active pointers, so a second finger turns a drag into a pinch mid-gesture.
const pointers = new Map();
let pinchStart = null;
let panStart = null;
let lastTap = 0;

const zoomed = computed(() => scale.value > 1.01);

const stageStyle = computed(() => ({
  transform: `translate3d(${offset.value.x}px, ${offset.value.y + dismissOffset.value}px, 0) scale(${scale.value})`,
  transition: animating.value ? `transform var(--motion)` : "none"
}));

// Fading the chrome out as the image is dragged away makes the dismiss feel
// like a direct manipulation rather than a button press.
const scrimStyle = computed(() => ({
  opacity: String(Math.max(0, 1 - dismissOffset.value / (DISMISS_DISTANCE * 2)))
}));

function reset() {
  scale.value = 1;
  offset.value = { x: 0, y: 0 };
  dismissOffset.value = 0;
  animating.value = true;
  pointers.clear();
  pinchStart = null;
  panStart = null;
}

function close() {
  emit("close");
}

function distanceBetween([a, b]) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function onPointerDown(event) {
  event.currentTarget.setPointerCapture?.(event.pointerId);
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  animating.value = false;

  if (pointers.size === 2) {
    pinchStart = {
      distance: distanceBetween([...pointers.values()]),
      scale: scale.value
    };
    panStart = null;
    return;
  }

  panStart = { x: event.clientX, y: event.clientY, offset: { ...offset.value } };
}

function onPointerMove(event) {
  if (!pointers.has(event.pointerId)) return;

  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

  if (pointers.size === 2 && pinchStart) {
    const ratio = distanceBetween([...pointers.values()]) / (pinchStart.distance || 1);
    scale.value = Math.min(MAX_SCALE, Math.max(1, pinchStart.scale * ratio));
    return;
  }

  if (!panStart) return;

  const deltaX = event.clientX - panStart.x;
  const deltaY = event.clientY - panStart.y;

  if (zoomed.value) {
    offset.value = { x: panStart.offset.x + deltaX, y: panStart.offset.y + deltaY };
    return;
  }

  // Not zoomed: a downward drag is a dismiss, and an upward one goes nowhere.
  dismissOffset.value = Math.max(0, deltaY);
}

function onPointerUp(event) {
  pointers.delete(event.pointerId);
  animating.value = true;

  if (pointers.size < 2) pinchStart = null;

  if (!zoomed.value) {
    if (dismissOffset.value > DISMISS_DISTANCE) {
      close();
      return;
    }

    dismissOffset.value = 0;
    offset.value = { x: 0, y: 0 };
  }

  if (pointers.size === 0) panStart = null;
}

function onTap(event) {
  const now = Date.now();

  if (now - lastTap < 300) {
    animating.value = true;
    scale.value = zoomed.value ? 1 : DOUBLE_TAP_SCALE;
    offset.value = { x: 0, y: 0 };
    lastTap = 0;
    event.preventDefault();
    return;
  }

  lastTap = now;
}

function onKeydown(event) {
  if (event.key === "Escape") close();
}

watch(() => props.open, (open) => {
  document.body.style.overflow = open ? "hidden" : "";

  if (open) {
    reset();
    window.addEventListener("keydown", onKeydown);
  } else {
    window.removeEventListener("keydown", onKeydown);
  }
});

onBeforeUnmount(() => {
  document.body.style.overflow = "";
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="viewer">
      <div v-if="open" class="viewer" role="dialog" aria-modal="true" :aria-label="title || 'Capture'">
        <header class="viewer-bar" :style="scrimStyle">
          <div class="viewer-title">
            <strong class="truncate">{{ title }}</strong>
            <small v-if="subtitle">{{ subtitle }}</small>
          </div>
          <button type="button" class="viewer-close" aria-label="Close viewer" @click="close">×</button>
        </header>

        <div
          class="viewer-stage"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @click="onTap"
        >
          <img :src="src" :alt="title || 'Capture'" :style="stageStyle" draggable="false" />
        </div>

        <footer v-if="$slots.actions" class="viewer-actions" :style="scrimStyle">
          <slot name="actions" />
        </footer>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* The one place a pure black background is right: it puts nothing next to the
 * photograph. */
.viewer {
  position: fixed;
  z-index: 240;
  inset: 0;
  display: grid;
  grid-template-rows: auto 1fr auto;
  background: #000000;
  touch-action: none;
}

.viewer-bar {
  display: flex;
  z-index: 1;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: calc(var(--safe-top) + var(--space-3)) var(--space-4) var(--space-3);
  background: linear-gradient(#000000b3, transparent);
  transition: opacity var(--motion-fast);
}

.viewer-title {
  display: grid;
  gap: 1px;
  min-width: 0;
  color: #ffffff;
}

.viewer-title strong {
  font-family: var(--font-mono);
  font-size: 13.5px;
  font-weight: 550;
}

.viewer-title small {
  color: #ffffffa6;
  font-size: 12px;
}

.viewer-close {
  width: var(--tap-target);
  min-height: var(--tap-target);
  flex: none;
  border: 0;
  border-radius: var(--radius-full);
  padding: 0;
  background: #ffffff1f;
  color: #ffffff;
  font-size: 22px;
  line-height: 1;
}

.viewer-close:hover:not(:disabled) {
  border-color: transparent;
  background: #ffffff2e;
}

.viewer-stage {
  display: grid;
  overflow: hidden;
  place-items: center;
  min-height: 0;
}

.viewer-stage img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  transform-origin: center;
  will-change: transform;
  -webkit-user-select: none;
  user-select: none;
}

.viewer-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4) calc(var(--safe-bottom) + var(--space-4));
  background: linear-gradient(transparent, #000000b3);
  transition: opacity var(--motion-fast);
}

.viewer-enter-active,
.viewer-leave-active {
  transition: opacity var(--motion);
}

.viewer-enter-from,
.viewer-leave-to {
  opacity: 0;
}
</style>
