<script setup>
import { computed, ref } from "vue";
import BottomSheet from "./BottomSheet.vue";
import ThemeToggle from "../ui/ThemeToggle.vue";
import { useSkyHub } from "../../composables/useSkyHub";

/* One row: identity, which camera, whether it is alive, and everything else
 * behind an overflow sheet. Anything that does not earn its width here belongs
 * in the sheet. */

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

const menuOpen = ref(false);

const capturing = computed(() => Boolean(settings.value?.capture_enabled));
const online = computed(() => Boolean(selectedNode.value?.online));

const CONNECTION = {
  live: { label: "Live", tone: "success" },
  connecting: { label: "Connecting", tone: "warning" },
  offline: { label: "Offline", tone: "danger" }
};

const connection = computed(() => CONNECTION[connectionState.value] || CONNECTION.offline);

function pickNode(nodeId) {
  selectNode(nodeId);
  menuOpen.value = false;
}

function refresh() {
  refreshDashboard();
  menuOpen.value = false;
}
</script>

<template>
  <header class="app-bar">
    <img class="app-bar-logo" src="/allskylogo.svg" alt="SkyHub" />

    <button type="button" class="app-bar-node" @click="menuOpen = true">
      <span class="dot" :class="online ? 'is-online' : 'is-offline'" />
      <span class="truncate">{{ selectedNodeId || "No node" }}</span>
      <span class="app-bar-caret" aria-hidden="true">⌄</span>
    </button>

    <span class="app-bar-status" :class="connection.tone">
      <span v-if="capturing" class="dot pulse" />
      {{ capturing ? "Capturing" : connection.label }}
    </span>

    <button
      type="button"
      class="app-bar-more"
      aria-label="More options"
      @click="menuOpen = true"
    >
      <span aria-hidden="true">⋯</span>
    </button>

    <BottomSheet :open="menuOpen" title="SkyHub" @close="menuOpen = false">
      <div class="sheet-section">
        <h3>Camera</h3>
        <button
          v-for="node in nodes"
          :key="node.node_id"
          type="button"
          class="sheet-row"
          :class="{ active: node.node_id === selectedNodeId }"
          @click="pickNode(node.node_id)"
        >
          <span class="dot" :class="node.online ? 'is-online' : 'is-offline'" />
          <span class="grow truncate">{{ node.node_id }}</span>
          <span class="sheet-row-note">{{ node.online ? "online" : "offline" }}</span>
        </button>
        <p v-if="!nodes.length" class="sheet-empty">No nodes have registered yet.</p>
      </div>

      <div class="sheet-section">
        <h3>Actions</h3>
        <button type="button" class="sheet-row" :disabled="loading" @click="refresh">
          <span class="sheet-row-icon" aria-hidden="true">↻</span>
          <span class="grow">{{ loading ? "Refreshing…" : "Refresh everything" }}</span>
        </button>
        <div class="sheet-row static">
          <span class="sheet-row-icon" aria-hidden="true">◐</span>
          <span class="grow">Appearance</span>
          <ThemeToggle />
        </div>
      </div>
    </BottomSheet>
  </header>
</template>

<style scoped>
.app-bar {
  position: sticky;
  z-index: 50;
  top: 0;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: calc(var(--appbar-height) + var(--safe-top));
  padding: var(--safe-top) var(--space-4) 0;
  border-bottom: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg) 80%, transparent);
  backdrop-filter: blur(20px) saturate(180%);
}

.app-bar-logo {
  width: 24px;
  height: 24px;
  flex: none;
}

.app-bar-node {
  display: flex;
  min-width: 0;
  max-width: 45vw;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  border: 0;
  border-radius: var(--radius-full);
  padding: 0 var(--space-3) 0 var(--space-2);
  background: var(--surface-raised);
  font-size: 14px;
  font-weight: 550;
}

.app-bar-caret {
  flex: none;
  color: var(--text-faint);
  font-size: 12px;
}

.app-bar-status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  color: var(--text-muted);
  font-size: 13px;
  white-space: nowrap;
}

.app-bar-status.success { color: var(--success); }
.app-bar-status.warning { color: var(--warning); }
.app-bar-status.danger { color: var(--danger); }

.app-bar-more {
  width: 40px;
  min-height: 40px;
  flex: none;
  border: 0;
  border-radius: var(--radius-full);
  padding: 0;
  background: transparent;
  color: var(--text-muted);
  font-size: 20px;
  line-height: 1;
}

.dot.is-online { color: var(--success); }
.dot.is-offline { color: var(--danger); }

/* On the narrowest phones the status word is the first thing to go: the dot on
 * the node button already says whether the camera is alive. */
@media (max-width: 359.98px) {
  .app-bar-status {
    display: none;
  }
}

.sheet-section {
  display: grid;
  gap: var(--space-1);
  padding-bottom: var(--space-5);
}

.sheet-section h3 {
  padding-bottom: var(--space-2);
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 500;
}

.sheet-row {
  display: flex;
  width: 100%;
  align-items: center;
  gap: var(--space-3);
  min-height: var(--tap-target);
  border: 0;
  border-radius: var(--radius);
  padding: 0 var(--space-3);
  background: transparent;
  font-size: 15px;
  font-weight: 450;
  text-align: left;
}

.sheet-row.active {
  background: var(--accent-soft);
  color: var(--accent);
}

.sheet-row.static {
  padding-right: var(--space-1);
}

.sheet-row-icon {
  width: 20px;
  flex: none;
  color: var(--text-faint);
  text-align: center;
}

.sheet-row-note {
  color: var(--text-faint);
  font-size: 13px;
}

.sheet-empty {
  padding: var(--space-3);
  color: var(--text-faint);
  font-size: 14px;
}
</style>
