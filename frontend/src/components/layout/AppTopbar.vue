<script setup>
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { authState, logout, markSignedOut } from "../../api/auth";
import { stopRealtime, useSkyHub } from "../../composables/useSkyHub";

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
const router = useRouter();

/* Signing out has to be reachable without hunting through Settings - it is the
 * one control someone reaches for in a hurry. It tolerates failure: if the call
 * does not land, the local state is cleared anyway, so the browser is never left
 * sitting in a UI it believes it is still signed in to. */
async function signOut() {
  try {
    await logout();
  } finally {
    markSignedOut();
    stopRealtime();
    router.replace("/login");
  }
}

const TITLES = {
  "/monitor": "Monitor",
  "/captures": "Captures",
  "/overlays": "Overlays",
  "/settings": "Settings",
  "/nodes": "Nodes"
};

const pageTitle = computed(() => TITLES[route.path] || "SkyHub");

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
      <strong>{{ pageTitle }}</strong>
    </div>

    <span class="topbar-spacer" />

    <div class="topbar-group">
      <!-- Connection and capture state read as one sentence rather than two pills. -->
      <span
        class="status-line"
        :class="connection.tone"
        :title="`Dashboard websocket: ${connection.label.toLowerCase()}`"
      >
        <span class="dot" :class="{ pulse: connection.pulse }" />
        {{ connection.label }}
      </span>

      <span
        class="status-line"
        :title="capturing ? 'Node is running a capture sequence' : 'No capture sequence running'"
      >
        {{ capturing ? "Capturing" : "Idle" }}
      </span>

      <label v-if="nodes.length" class="node-picker">
        <!-- The node's own online state lives on the picker, so it needs no badge. -->
        <span
          class="dot"
          :style="{ color: selectedNode?.online ? 'var(--success)' : 'var(--danger)' }"
          :title="selectedNode?.online ? 'Node online' : 'Node offline'"
        />
        <select v-model="currentNode" aria-label="Selected node">
          <option v-for="node in nodes" :key="node.node_id" :value="node.node_id">
            {{ node.node_id }}{{ node.online ? "" : " (offline)" }}
          </option>
        </select>
      </label>

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

      <button
        type="button"
        class="icon ghost"
        :title="`Sign out ${authState.username || ''}`.trim()"
        aria-label="Sign out"
        @click="signOut"
      >
        <span aria-hidden="true">⏻</span>
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
