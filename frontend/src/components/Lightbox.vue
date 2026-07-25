<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { captureUrl, formatBytes, formatDateTime } from "../api/skyhub";

const props = defineProps({
  capture: { type: Object, default: null },
  hasPrevious: { type: Boolean, default: false },
  hasNext: { type: Boolean, default: false }
});

const emit = defineEmits(["close", "previous", "next"]);

const showOriginal = ref(false);
const loaded = ref(false);

// The rendered file has the overlay burned in and has been re-encoded; the
// original is the untouched frame the node uploaded.
const imageUrl = computed(() => (
  props.capture ? captureUrl(props.capture, { raw: showOriginal.value }) : null
));

const canShowOriginal = computed(() => Boolean(props.capture?.original_available));

function handleKeydown(event) {
  if (!props.capture) return;

  if (event.key === "Escape") emit("close");
  else if (event.key === "ArrowLeft" && props.hasPrevious) emit("previous");
  else if (event.key === "ArrowRight" && props.hasNext) emit("next");
  else if (event.key.toLowerCase() === "o" && canShowOriginal.value) {
    showOriginal.value = !showOriginal.value;
  }
}

watch(() => props.capture, (capture) => {
  showOriginal.value = false;
  loaded.value = false;

  if (capture) {
    window.addEventListener("keydown", handleKeydown);
    // Stop the page behind the overlay scrolling while it is open.
    document.body.style.overflow = "hidden";
  } else {
    window.removeEventListener("keydown", handleKeydown);
    document.body.style.overflow = "";
  }
}, { immediate: true });

watch(imageUrl, () => { loaded.value = false; });

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleKeydown);
  document.body.style.overflow = "";
});
</script>

<template>
  <Transition name="fade">
    <div
      v-if="capture"
      class="lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="Capture viewer"
    >
      <div class="lightbox-header">
        <div class="lightbox-header-text">
          <strong class="truncate">{{ capture.filename }}</strong>
          <small>
            {{ formatDateTime(capture.captured_at || capture.modified_at) }} ·
            {{ capture.width }}×{{ capture.height }} ·
            {{ formatBytes(capture.size_bytes) }} ·
            {{ showOriginal ? "original" : "rendered" }}
          </small>
        </div>
        <div class="row">
          <button
            v-if="canShowOriginal"
            type="button"
            :class="{ active: showOriginal }"
            title="Toggle between the rendered and original frame (O)"
            @click="showOriginal = !showOriginal"
          >
            {{ showOriginal ? "Original" : "Rendered" }}
          </button>
          <a class="btn" :href="imageUrl" download :title="`Download ${capture.filename}`">
            Download
          </a>
          <button type="button" :disabled="!hasPrevious" @click="$emit('previous')">Prev</button>
          <button type="button" :disabled="!hasNext" @click="$emit('next')">Next</button>
          <button type="button" class="icon" aria-label="Close viewer" @click="$emit('close')">
            ×
          </button>
        </div>
      </div>

      <div class="lightbox-stage" @click.self="$emit('close')">
        <button
          v-if="hasPrevious"
          class="lightbox-nav previous"
          type="button"
          aria-label="Previous capture"
          @click="$emit('previous')"
        >
          ‹
        </button>

        <div v-if="!loaded" class="skeleton" style="width: 60%; aspect-ratio: 4/3" />
        <img
          :src="imageUrl"
          :alt="capture.filename"
          :style="{ display: loaded ? 'block' : 'none' }"
          @load="loaded = true"
          @error="loaded = true"
        />

        <button
          v-if="hasNext"
          class="lightbox-nav next"
          type="button"
          aria-label="Next capture"
          @click="$emit('next')"
        >
          ›
        </button>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
