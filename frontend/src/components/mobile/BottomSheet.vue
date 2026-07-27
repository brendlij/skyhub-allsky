<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";

/* A sheet that slides up over the page, dismissed by the grab handle, a tap on
 * the scrim, Escape, or a downward drag. It exists so detail can be reached
 * without navigating away from the live image. */

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: "" }
});

const emit = defineEmits(["close"]);

const dragOffset = ref(0);
const dragging = ref(false);
let startY = 0;

// Dragging is a transform, never a layout property, so the sheet stays on the
// compositor while it follows the finger.
const sheetStyle = computed(() => (
  dragOffset.value
    ? { transform: `translateY(${dragOffset.value}px)`, transition: dragging.value ? "none" : null }
    : {}
));

function close() {
  emit("close");
}

function onPointerDown(event) {
  dragging.value = true;
  startY = event.clientY;
  event.currentTarget.setPointerCapture?.(event.pointerId);
}

function onPointerMove(event) {
  if (!dragging.value) return;

  // Downward only: dragging up would peel the sheet off the bottom of the screen.
  dragOffset.value = Math.max(0, event.clientY - startY);
}

function onPointerUp() {
  if (!dragging.value) return;

  dragging.value = false;

  // Far enough to read as "put it away" rather than a stray touch.
  if (dragOffset.value > 110) {
    close();
  }

  dragOffset.value = 0;
}

function onKeydown(event) {
  if (event.key === "Escape") close();
}

// The page behind must not scroll while the sheet is up, or dismissing it leaves
// the user somewhere else entirely.
watch(() => props.open, (open) => {
  document.body.style.overflow = open ? "hidden" : "";

  if (open) {
    window.addEventListener("keydown", onKeydown);
  } else {
    window.removeEventListener("keydown", onKeydown);
    dragOffset.value = 0;
  }
});

onBeforeUnmount(() => {
  document.body.style.overflow = "";
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="sheet">
      <div v-if="open" class="sheet-scrim" @click.self="close">
        <section
          class="sheet"
          role="dialog"
          aria-modal="true"
          :aria-label="title || 'Details'"
          :style="sheetStyle"
        >
          <div
            class="sheet-grip"
            @pointerdown="onPointerDown"
            @pointermove="onPointerMove"
            @pointerup="onPointerUp"
            @pointercancel="onPointerUp"
          >
            <span class="sheet-handle" aria-hidden="true" />
          </div>

          <header v-if="title || $slots.actions" class="sheet-header">
            <h2>{{ title }}</h2>
            <slot name="actions" />
          </header>

          <div class="sheet-body">
            <slot />
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.sheet-scrim {
  position: fixed;
  z-index: 220;
  inset: 0;
  display: flex;
  align-items: flex-end;
  background: #00000073;
  backdrop-filter: blur(2px);
}

.sheet {
  display: flex;
  overflow: hidden;
  flex-direction: column;
  width: 100%;
  max-height: min(88dvh, 760px);
  border-top-left-radius: var(--radius-sheet);
  border-top-right-radius: var(--radius-sheet);
  padding-bottom: var(--safe-bottom);
  background: var(--surface);
  box-shadow: var(--shadow-lg);
  transition: transform var(--motion);
}

/* On a tablet or desktop the sheet stops spanning the whole width, or it reads
 * as a page that lost its layout. */
@media (min-width: 600px) {
  .sheet-scrim {
    align-items: center;
    justify-content: center;
    padding: var(--space-6);
  }

  .sheet {
    max-width: 560px;
    border-radius: var(--radius-sheet);
  }
}

.sheet-grip {
  display: grid;
  flex: none;
  place-items: center;
  padding: var(--space-3) 0 var(--space-2);
  cursor: grab;
  touch-action: none;
}

.sheet-handle {
  width: 40px;
  height: 4px;
  border-radius: var(--radius-full);
  background: var(--border-strong);
}

.sheet-header {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 0 var(--space-5) var(--space-3);
}

.sheet-header h2 {
  font-size: var(--text-title);
}

.sheet-body {
  overflow-y: auto;
  padding: 0 var(--space-5) var(--space-6);
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}

.sheet-enter-active,
.sheet-leave-active {
  transition: opacity var(--motion);
}

.sheet-enter-active .sheet,
.sheet-leave-active .sheet {
  transition: transform var(--motion);
}

.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}

.sheet-enter-from .sheet,
.sheet-leave-to .sheet {
  transform: translateY(100%);
}
</style>
