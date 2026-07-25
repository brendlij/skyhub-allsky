<script setup>
import { computed } from "vue";
import OverlayEditor from "../components/OverlayEditor.vue";
import EmptyState from "../components/ui/EmptyState.vue";
import { captureUrl } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";

const { busy, latest, overlaySettings, saveOverlays, selectedNodeId } = useSkyHub();

// The editor previews against the untouched original, so a saved overlay is not
// baked into the image you are positioning the next one on.
const previewUrl = computed(() => (latest.value ? captureUrl(latest.value, { raw: true }) : null));

const enabledCount = computed(() => (
  overlaySettings.value?.entities?.filter((entity) => entity.enabled).length || 0
));
</script>

<template>
  <div class="stack-lg">
    <div class="page-head">
      <div class="page-head-text">
        <h1>Overlays</h1>
        <p>
          <template v-if="overlaySettings">
            {{ enabledCount }} of {{ overlaySettings.entities?.length || 0 }} labels shown ·
            {{ overlaySettings.enabled ? "burned into saved captures" : "disabled" }}
          </template>
        </p>
      </div>
      <div class="page-head-actions">
        <button
          type="button"
          class="primary"
          :disabled="!overlaySettings || busy.overlays"
          @click="saveOverlays"
        >
          {{ busy.overlays ? "Saving…" : "Save overlays" }}
        </button>
      </div>
    </div>

    <div v-if="!overlaySettings" class="panel">
      <EmptyState icon="◫" title="No node selected" message="Pick a node in the top bar to edit its overlays." />
    </div>

    <OverlayEditor
      v-else
      :overlays="overlaySettings"
      :image-url="previewUrl"
      :node-id="selectedNodeId"
    />
  </div>
</template>
