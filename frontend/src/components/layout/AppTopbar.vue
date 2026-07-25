<script setup>
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useSkyHub } from "../../composables/useSkyHub";

const {
  connectionState,
  loading,
  nodes,
  refreshDashboard,
  selectNode,
  selectedNode,
  selectedNodeId,
  settings
} = useSkyHub();

const route = useRoute();

const TITLES = {
  "/monitor": ["Monitor", "Live view and node telemetry"],
  "/captures": ["Captures", "Browse the archive by night"],
  "/overlays": ["Overlays", "Text burned into saved captures"],
  "/settings": ["Settings", "Camera, colour, hardware and storage"],
  "/nodes": ["Nodes", "Connected camera nodes"]
};

const pageTitle = computed(() => TITLES[route.path] || ["SkyHub", ""]);

const CONNECTION = {
  live: { label: "Live", tone: "success", pulse: true },
  connecting: { label: "Connecting", tone: "warning", pulse: true },
  offline: { label: "Offline", tone: "danger", pulse: false }
};

const connection = computed(() => CONNECTION[connectionState.value] || CONNECTION.offline);
const capturing = computed(() => Boolean(settings.value?.capture_enabled));

// The select is bound to a local proxy so selectNode owns the state change; a
// v-model straight onto selectedNodeId mutated it before the loaders had run.
const currentNode = computed({
  get: () => selectedNodeId.value || "",
  set: (value) => { selectNode(value); }
});
</script>

<template>
  <header class="topbar">
    <div class="topbar-title">
      <strong>{{ pageTitle[0] }}</strong>
      <small>{{ pageTitle[1] }}</small>
    </div>

    <span class="topbar-spacer" />

    <div class="topbar-group">
      <span
        class="badge"
        :class="capturing ? 'accent' : ''"
        :title="capturing ? 'Node is running a capture sequence' : 'No capture sequence running'"
      >
        <span class="dot" :class="{ pulse: capturing }" />
        {{ capturing ? "Capturing" : "Idle" }}
      </span>

      <span
        class="badge"
        :class="connection.tone"
        :title="`Dashboard websocket: ${connection.label.toLowerCase()}`"
      >
        <span class="dot" :class="{ pulse: connection.pulse }" />
        {{ connection.label }}
      </span>

      <label v-if="nodes.length" class="node-picker">
        <span>Node</span>
        <select v-model="currentNode" aria-label="Selected node">
          <option v-for="node in nodes" :key="node.node_id" :value="node.node_id">
            {{ node.node_id }}{{ node.online ? "" : " (offline)" }}
          </option>
        </select>
      </label>

      <span
        v-if="selectedNode"
        class="badge"
        :class="selectedNode.online ? 'success' : 'danger'"
      >
        {{ selectedNode.online ? "online" : "offline" }}
      </span>

      <button
        type="button"
        class="icon ghost"
        :disabled="loading"
        title="Refresh everything"
        aria-label="Refresh everything"
        @click="refreshDashboard"
      >
        <span :class="{ spin: loading }" aria-hidden="true">↻</span>
      </button>
    </div>
  </header>
</template>

<style scoped>
.spin {
  display: inline-block;
  animation: spin 900ms linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
