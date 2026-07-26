<script setup>
import { computed, ref, watch } from "vue";
import { formatBytes, requestJson, withApiKey } from "../api/skyhub";
import { confirmAction } from "../composables/useConfirm";
import { useSkyHub } from "../composables/useSkyHub";
import { useToasts } from "../composables/useToasts";

const { latest, latestImageUrl, selectedNodeId } = useSkyHub();
const { notify, notifyError } = useToasts();

const mask = ref(null);
const busy = ref(false);
const fileInput = ref(null);
// The mask always lives at the same URL, so a re-upload needs a new query to
// get past the browser cache.
const version = ref(Date.now());

const maskImageUrl = computed(() => (
  mask.value?.exists && selectedNodeId.value
    ? withApiKey(`/api/nodes/${selectedNodeId.value}/mask/image?v=${version.value}`)
    : null
));

// A mask drawn at a different resolution still works - it is scaled to the frame
// - but it is worth saying so, because a scaled mask has soft edges.
const scaled = computed(() => {
  const capture = latest.value;

  if (!mask.value?.exists || !capture?.width || !mask.value.width) return false;

  return mask.value.width !== capture.width || mask.value.height !== capture.height;
});

async function loadMask() {
  if (!selectedNodeId.value) {
    mask.value = null;
    return;
  }

  try {
    mask.value = await requestJson(`/api/nodes/${selectedNodeId.value}/mask`);

  } catch (error) {
    notifyError(error);
  }
}

watch(selectedNodeId, loadMask, { immediate: true });

async function uploadMask(event) {
  const file = event.target.files?.[0];

  if (!file || !selectedNodeId.value) return;

  const body = new FormData();
  body.append("file", file);
  busy.value = true;

  try {
    mask.value = await requestJson(`/api/nodes/${selectedNodeId.value}/mask`, {
      method: "POST",
      body
    });

    version.value = Date.now();
    // Say how the file was read: a mask that was interpreted the other way round
    // is the one mistake that looks like the feature is broken.
    notify(
      mask.value.mode === "black-and-white"
        ? "Mask saved — read as black and white, white kept. Applies from the next capture."
        : "Mask saved — read by transparency, transparent kept. Applies from the next capture."
    );

  } catch (error) {
    notifyError(error);

  } finally {
    busy.value = false;
    // Clearing lets the same file be picked again after a failed upload.
    event.target.value = "";
  }
}

async function removeMask() {
  const confirmed = await confirmAction({
    title: "Remove the mask?",
    message: "New captures keep their full frame again. Captures already masked stay as they are.",
    confirmLabel: "Remove",
    tone: "danger"
  });

  if (!confirmed) return;

  busy.value = true;

  try {
    mask.value = await requestJson(`/api/nodes/${selectedNodeId.value}/mask`, {
      method: "DELETE"
    });

  } catch (error) {
    notifyError(error);

  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <h2>
        Mask
        <span v-if="mask?.exists" class="badge success">active</span>
      </h2>
    </div>

    <div class="panel-body">
      <p class="field-hint">
        A PNG the shape of the frame, marking what to black out — a roof, a street
        lamp, the corners outside a fisheye circle. Draw it either way: with
        transparency, where transparent is kept and opaque is covered, or flat
        black and white, where white is kept and black is covered.
      </p>

      <div class="mask-preview">
        <img v-if="latestImageUrl" :src="latestImageUrl" alt="Latest capture" />
        <div v-else class="mask-preview-empty">No capture to preview against yet.</div>
        <img v-if="maskImageUrl" class="mask-preview-layer" :src="maskImageUrl" alt="Mask" />
      </div>

      <dl v-if="mask?.exists" class="data-list">
        <div class="data-row">
          <dt>Size</dt>
          <dd class="data-value mono">{{ mask.width }}×{{ mask.height }}</dd>
        </div>
        <div class="data-row">
          <dt>File</dt>
          <dd class="data-value mono">{{ formatBytes(mask.size_bytes) }}</dd>
        </div>
      </dl>

      <p v-if="scaled" class="callout warning">
        The mask is {{ mask.width }}×{{ mask.height }} but the frame is
        {{ latest.width }}×{{ latest.height }}, so it is scaled to fit and its
        edges will be soft. Export it at the frame size for a clean cut.
      </p>

      <p class="callout warning">
        The mask is burned in before the original is stored, so it covers the raw
        copy as well. Whatever it hides cannot be recovered from the server.
      </p>

      <input
        ref="fileInput"
        class="visually-hidden"
        type="file"
        accept="image/png,.png"
        @change="uploadMask"
      />

      <div class="row wrap">
        <button
          type="button"
          class="primary"
          :disabled="busy || !selectedNodeId"
          @click="fileInput.click()"
        >
          {{ busy ? "Uploading…" : mask?.exists ? "Replace mask" : "Upload mask" }}
        </button>
        <button v-if="mask?.exists" type="button" class="danger" :disabled="busy" @click="removeMask">
          Remove
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.mask-preview {
  position: relative;
  display: grid;
  overflow: hidden;
  place-items: center;
  min-height: 160px;
  border-radius: var(--radius);
  background: var(--image-backdrop);
}

.mask-preview img {
  width: 100%;
  max-height: 340px;
  object-fit: contain;
}

/* Sits exactly on the capture, so alignment can be judged by eye. */
.mask-preview-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.mask-preview-empty {
  padding: var(--space-6);
  color: var(--text-faint);
  font-size: 13.5px;
  text-align: center;
}
</style>
