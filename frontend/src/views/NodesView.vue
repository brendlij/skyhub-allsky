<script setup>
import EmptyState from "../components/ui/EmptyState.vue";
import { formatDateTime } from "../api/skyhub";
import { useSkyHub } from "../composables/useSkyHub";

const { deleteNode, nodes, selectedNodeId, selectNode } = useSkyHub();
</script>

<template>
  <div class="stack-lg">
    <div class="page-head">
      <div class="page-head-text">
        <h1>Nodes</h1>
        <p>
          {{ nodes.filter((node) => node.online).length }} of {{ nodes.length }} online
        </p>
      </div>
    </div>

    <div v-if="!nodes.length" class="panel">
      <EmptyState
        icon="⬡"
        title="No nodes registered"
        message="Run `python skyhub.py node` on a Pi pointed at this server. It registers itself on first connect."
      />
    </div>

    <div v-else class="node-cards">
      <article
        v-for="node in nodes"
        :key="node.node_id"
        class="node-card"
        :class="{ active: node.node_id === selectedNodeId }"
      >
        <div class="node-card-head">
          <strong>{{ node.node_id }}</strong>
          <span class="badge" :class="node.online ? 'success' : 'danger'">
            <span class="dot" :class="{ pulse: node.online }" />
            {{ node.online ? "online" : "offline" }}
          </span>
        </div>

        <dl>
          <dt>Camera</dt>
          <dd>{{ node.capabilities?.camera || "—" }}</dd>
          <dt>Sensor</dt>
          <dd>{{ node.capabilities?.camera_name || "—" }}</dd>
          <dt>Last event</dt>
          <dd>{{ node.last_message_type || "—" }}</dd>
          <dt>Last seen</dt>
          <dd>{{ node.last_seen_at ? formatDateTime(node.last_seen_at) : "never" }}</dd>
        </dl>

        <div class="node-card-actions">
          <button
            type="button"
            class="grow"
            :class="{ primary: node.node_id !== selectedNodeId }"
            :disabled="node.node_id === selectedNodeId"
            @click="selectNode(node.node_id)"
          >
            {{ node.node_id === selectedNodeId ? "Selected" : "Select" }}
          </button>
          <button
            type="button"
            class="danger"
            :disabled="node.online"
            :title="node.online ? 'Stop the node before deleting it' : `Delete ${node.node_id}`"
            @click="deleteNode(node.node_id)"
          >
            Delete
          </button>
        </div>
      </article>
    </div>
  </div>
</template>
