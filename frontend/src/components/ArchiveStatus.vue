<script setup>
import { computed, onMounted, ref } from "vue";
import { requestJson } from "../api/skyhub";
import { useToasts } from "../composables/useToasts";

/* Read-only view of the archive. It is configured on the server with
 * SKYHUB_SERVER_ARCHIVE_DIR (see docs/ARCHIVE.md), not here, because the path is a
 * mount on the Pi and a typo in a form must not be able to point the cleanup at the
 * wrong disk. */

const { notify, notifyError } = useToasts();

const status = ref(null);
const running = ref(false);

async function load() {
  try {
    status.value = await requestJson("/api/archive/status");
  } catch (error) {
    notifyError(error);
  }
}

async function runNow() {
  running.value = true;

  try {
    const result = await requestJson("/api/archive/run", { method: "POST" });

    if (result.skipped) {
      notify(`Archive not run: ${result.skipped}`);
    } else if (result.error) {
      notify(`Archive failed: ${result.error}`);
    } else {
      notify(`Archived ${result.copied_files} files, freed ${result.deleted_files}.`);
    }

    await load();
  } catch (error) {
    notifyError(error);
  } finally {
    running.value = false;
  }
}

onMounted(load);

const gb = (value) => (typeof value === "number" ? `${value.toFixed(1)} GB` : "—");

const lastRun = computed(() => {
  const at = status.value?.last_run_at;

  return at ? new Date(at).toLocaleString() : "not yet";
});
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <h2>
        Archive
        <span v-if="status?.configured" class="badge" :class="status.ready ? 'success' : 'warning'">
          {{ status.ready ? "ready" : "unavailable" }}
        </span>
      </h2>
    </div>

    <div class="panel-body">
      <p v-if="!status" class="muted">Loading…</p>

      <p v-else-if="!status.configured" class="field-hint">
        No archive is set up. Frames stay on this disk; capture pauses when it is nearly full.
        Mount a NAS or drive and set <code>SKYHUB_SERVER_ARCHIVE_DIR</code> to copy frames off
        automatically — see <code>docs/ARCHIVE.md</code>.
      </p>

      <template v-else>
        <p v-if="status.problem" class="callout warning">{{ status.problem }}</p>

        <div class="storage-grid">
          <div class="storage-item"><span>Destination</span><strong>{{ status.archive_dir }}</strong></div>
          <div class="storage-item"><span>Local free</span><strong>{{ gb(status.free_gb_local) }}</strong></div>
          <div class="storage-item"><span>Archive free</span><strong>{{ gb(status.free_gb_archive) }}</strong></div>
          <div class="storage-item"><span>Files archived</span><strong>{{ status.archived_files }}</strong></div>
          <div class="storage-item"><span>Last run</span><strong>{{ lastRun }}</strong></div>
        </div>

        <p class="field-hint">
          Local copies are deleted only after the archive copy was read back and verified —
          oldest day first, once free space falls below {{ status.free_trigger_gb }} GB, until it
          is back above {{ status.free_target_gb }} GB. Today's frames are never removed.
        </p>
      </template>
    </div>

    <div v-if="status?.configured" class="panel-footer">
      <button type="button" class="primary" :disabled="running || !status.ready" @click="runNow">
        {{ running ? "Running…" : "Archive now" }}
      </button>
    </div>
  </section>
</template>
