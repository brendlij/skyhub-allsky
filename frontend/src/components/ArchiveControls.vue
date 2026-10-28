<script setup>
import { computed, onMounted, ref } from "vue";
import { requestJson } from "../api/skyhub";
import { useToasts } from "../composables/useToasts";

/* The whole archive is configured here. The destination is a folder on this
 * machine - for a NAS that means a share mounted into the filesystem first (see
 * docs/ARCHIVE.md); the mounts found on the machine are offered below so it can be
 * picked rather than typed. The archive folder is only ever copied *to*: nothing
 * here can delete anything inside it. */

const { notify, notifyError } = useToasts();

const JSON_HEADERS = { "Content-Type": "application/json" };

const SOURCE_OPTIONS = [
  { id: "originals", label: "Original frames", hint: "The unprocessed copy of every frame." },
  { id: "captures", label: "Rendered frames", hint: "Frames with overlay and colour applied." },
  { id: "derived", label: "Timelapses, keograms, startrails", hint: "Copied only, never removed from this disk." }
];

const form = ref(null);
const saved = ref(null);
const status = ref(null);
const mounts = ref([]);
const testResult = ref(null);
const busy = ref({ save: false, test: false, run: false });

const snapshot = (value) => (value ? JSON.stringify(value) : null);
const dirty = computed(() => Boolean(form.value) && snapshot(form.value) !== saved.value);

async function load() {
  try {
    const [settings, state, found] = await Promise.all([
      requestJson("/api/archive/settings"),
      requestJson("/api/archive/status"),
      requestJson("/api/archive/mounts")
    ]);

    form.value = settings;
    saved.value = snapshot(settings);
    status.value = state;
    mounts.value = found.mounts || [];
  } catch (error) {
    notifyError(error);
  }
}

async function refreshStatus() {
  try {
    status.value = await requestJson("/api/archive/status");
  } catch (error) {
    notifyError(error);
  }
}

async function save() {
  busy.value.save = true;

  try {
    const result = await requestJson("/api/archive/settings", {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify(form.value)
    });

    form.value = result;
    saved.value = snapshot(result);
    notify("Archive settings saved.");
    await refreshStatus();
  } catch (error) {
    // The server explains exactly what is wrong (folder missing, not writable...).
    notifyError(error);
  } finally {
    busy.value.save = false;
  }
}

async function testDestination() {
  busy.value.test = true;
  testResult.value = null;

  try {
    testResult.value = await requestJson("/api/archive/test", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ path: form.value.archive_dir, require_mount: form.value.require_mount })
    });
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value.test = false;
  }
}

async function runNow() {
  busy.value.run = true;

  try {
    const result = await requestJson("/api/archive/run", { method: "POST" });

    if (result.skipped) notify(`Not run: ${result.skipped}`, { tone: "error" });
    else if (result.error) notify(`Failed: ${result.error}`, { tone: "error" });
    else notify(`Copied ${result.copied_files} files, removed ${result.deleted_files} local copies.`);

    await refreshStatus();
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value.run = false;
  }
}

function useMount(path) {
  form.value.archive_dir = path;
  testResult.value = null;
}

function toggleSource(id, on) {
  const next = new Set(form.value.sources);

  if (on) next.add(id);
  else next.delete(id);

  form.value.sources = SOURCE_OPTIONS.map((option) => option.id).filter((name) => next.has(name));
}

onMounted(load);

const gb = (value) => (typeof value === "number" ? `${value.toFixed(1)} GB` : "-");
const lastRun = computed(() => (status.value?.last_run_at ? new Date(status.value.last_run_at).toLocaleString() : "not yet"));

const badge = computed(() => {
  if (!status.value) return null;
  if (!status.value.enabled) return { tone: "", text: "off" };

  return status.value.ready ? { tone: "success", text: "running" } : { tone: "warning", text: "unavailable" };
});
</script>

<template>
  <section class="panel archive-panel">
    <div class="panel-header">
      <h2>
        Archive
        <span v-if="badge" class="badge" :class="badge.tone">{{ badge.text }}</span>
        <span v-if="dirty" class="badge warning">unsaved</span>
      </h2>
    </div>

    <div v-if="!form" class="panel-body"><p class="field-hint">Loading…</p></div>

    <div v-else class="panel-body">
      <p class="field-hint">
        Copies frames to another disk or a network share, checks every copy, and only then frees
        space here. Nothing is deleted from this disk unless a verified copy exists.
      </p>

      <label class="check">
        <input v-model="form.enabled" type="checkbox" />
        Archive frames automatically
      </label>

      <div class="section">
        <div class="section-title">Destination</div>

        <label class="field">
          <span>Folder on this machine</span>
          <input v-model="form.archive_dir" type="text" placeholder="/mnt/nas/allsky" spellcheck="false" />
        </label>

        <div v-if="mounts.length" class="mount-chips">
          <button
            v-for="mount in mounts"
            :key="mount.path"
            type="button"
            :title="`${mount.source} · ${mount.type}`"
            @click="useMount(mount.path)"
          >
            {{ mount.path }} · {{ mount.free_gb }} GB free
          </button>
        </div>
        <p v-else class="field-hint">
          No network share or extra drive is mounted on this machine. Mount the NAS first
          (see docs/ARCHIVE.md); it will then show up here.
        </p>

        <div v-if="testResult" class="test-result">
          <div v-for="check in testResult.checks" :key="check.label" :class="check.ok ? 'ok' : 'bad'">
            {{ check.ok ? "Passed" : "Failed" }} · {{ check.label }}
            <span class="field-hint"> {{ check.detail }}</span>
          </div>
        </div>

        <div>
          <button type="button" :disabled="busy.test || !form.archive_dir" @click="testDestination">
            {{ busy.test ? "Testing…" : "Test destination" }}
          </button>
        </div>
      </div>

      <div class="section">
        <div class="section-title">What to copy</div>
        <label v-for="option in SOURCE_OPTIONS" :key="option.id" class="check">
          <input
            type="checkbox"
            :checked="form.sources.includes(option.id)"
            @change="toggleSource(option.id, $event.target.checked)"
          />
          <span>{{ option.label }} <small class="field-hint">{{ option.hint }}</small></span>
        </label>
      </div>

      <div class="section">
        <div class="section-title">Free up space</div>
        <div class="field-grid">
          <label class="field">
            <span>Start when free space drops below <em class="field-unit">GB</em></span>
            <input v-model.number="form.free_trigger_gb" type="number" min="0.5" step="0.5" />
          </label>
          <label class="field">
            <span>Keep going until free space reaches <em class="field-unit">GB</em></span>
            <input v-model.number="form.free_target_gb" type="number" min="1" step="0.5" />
          </label>
          <label class="field">
            <span>Also remove local copies older than <em class="field-unit">days, 0 = off</em></span>
            <input v-model.number="form.delete_after_days" type="number" min="0" />
          </label>
          <label class="field">
            <span>Check every <em class="field-unit">minutes</em></span>
            <input v-model.number="form.interval_minutes" type="number" min="1" max="1440" />
          </label>
        </div>
        <p class="field-hint">
          Oldest day first. Today's frames, and any day still being processed, are never removed.
          If the archive cannot be reached, nothing is removed and capture pauses before the disk fills.
        </p>
      </div>

      <div class="section">
        <div class="section-title">Safety</div>
        <label class="check">
          <input v-model="form.require_mount" type="checkbox" />
          <span>
            Require a separate disk
            <small class="field-hint">
              Refuses a folder on this machine's own disk. A share that is not mounted looks like an
              empty folder here, and copying into it would fill the card.
            </small>
          </span>
        </label>
      </div>

      <div v-if="status" class="section">
        <div class="section-title">Status</div>
        <p v-if="status.problem && form.enabled" class="callout warning">{{ status.problem }}</p>
        <p v-if="status.last_error && !status.problem" class="callout warning">{{ status.last_error }}</p>
        <div class="storage-grid">
          <div class="storage-item"><span>Free here</span><strong>{{ gb(status.free_gb_local) }}</strong></div>
          <div class="storage-item"><span>Free on archive</span><strong>{{ gb(status.free_gb_archive) }}</strong></div>
          <div class="storage-item"><span>Files archived</span><strong>{{ status.archived_files }}</strong></div>
          <div class="storage-item"><span>Last run</span><strong>{{ lastRun }}</strong></div>
        </div>
      </div>
    </div>

    <div v-if="form" class="panel-footer archive-footer">
      <button type="button" class="primary" :disabled="!dirty || busy.save" @click="save">
        {{ busy.save ? "Saving…" : "Save archive settings" }}
      </button>
      <button type="button" :disabled="busy.run || !form.archive_dir || dirty" @click="runNow">
        {{ busy.run ? "Running…" : "Run now" }}
      </button>
    </div>
  </section>
</template>
