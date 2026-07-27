<script setup>
import { onMounted, reactive, ref } from "vue";
import { requestJson } from "../api/skyhub";
import { useProcessing } from "../composables/useProcessing";
import { useToasts } from "../composables/useToasts";

/* The controls are built from what the server says each processor accepts, not
 * from a list written here. A processor added later - meteor detection, cloud
 * classification - declares its fields in Python and gets a working settings
 * panel without this file changing. That is the point of the whole registry.
 */

const { status, loadStatus, updateProcessor } = useProcessing();
const { notify, notifyError } = useToasts();

const drafts = reactive({});
const busy = ref("");
const expanded = ref(null);

const retention = ref([]);
const retentionDrafts = reactive({});
const sweepResult = ref(null);

onMounted(async () => {
  try {
    await loadStatus();
    seed();
    await loadRetention();
  } catch (error) {
    notifyError(error);
  }
});

async function loadRetention() {
  const result = await requestJson("/api/processing/retention");

  retention.value = result.policies || [];

  for (const policy of retention.value) {
    retentionDrafts[policy.category] = {
      keep_days: policy.keep_days,
      max_gb: policy.max_gb
    };
  }
}

async function saveRetention(policy) {
  busy.value = `retention:${policy.category}`;

  try {
    await requestJson(`/api/processing/retention/${policy.category}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(retentionDrafts[policy.category])
    });

    await loadRetention();
    notify(`Retention saved for ${policy.category}`);
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = "";
  }
}

/* Always a dry run from here. Retention deletes finished work, so the button
 * reports what would go and the operator decides - a one-click irreversible
 * sweep behind a settings panel is not a kindness. */
async function previewSweep() {
  busy.value = "sweep";

  try {
    sweepResult.value = await requestJson("/api/processing/retention/apply?dry_run=true", {
      method: "POST"
    });
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = "";
  }
}

function seed() {
  for (const processor of status.value?.processors || []) {
    drafts[processor.name] = {
      enabled: processor.enabled,
      priority: processor.priority,
      config: { ...processor.config }
    };
  }
}

function isDirty(processor) {
  const draft = drafts[processor.name];

  if (!draft) return false;
  if (draft.enabled !== processor.enabled || draft.priority !== processor.priority) return true;

  return Object.keys(draft.config).some((key) => draft.config[key] !== processor.config[key]);
}

async function save(processor) {
  busy.value = processor.name;

  try {
    await updateProcessor(processor.name, drafts[processor.name]);
    seed();
    notify(`${processor.label} saved`);
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = "";
  }
}

async function toggle(processor) {
  drafts[processor.name].enabled = !drafts[processor.name].enabled;
  await save(processor);
}
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <h2>
        Processing
        <span v-if="status?.pipeline?.running" class="badge success">running</span>
        <span v-else class="badge warning">stopped</span>
      </h2>
    </div>

    <div class="panel-body">
      <p class="field-hint">
        Each processor watches new captures independently and builds its product as the
        night runs, so nothing is reprocessed at sunrise. Turning one off stops it from
        the next capture; it does not remove what it already made.
      </p>

      <dl v-if="status" class="data-list">
        <div class="data-row">
          <dt>Queue</dt>
          <dd class="data-value mono">
            {{ status.pipeline.queued }} / {{ status.pipeline.queue_size }}
            <template v-if="status.pipeline.dropped">
              · {{ status.pipeline.dropped }} dropped
            </template>
          </dd>
        </div>
        <div class="data-row">
          <dt>Processed</dt>
          <dd class="data-value mono">{{ status.pipeline.processed }} frames</dd>
        </div>
        <div class="data-row">
          <dt>Video encoder</dt>
          <dd class="data-value">
            <template v-if="status.ffmpeg.available">ffmpeg found</template>
            <template v-else>Not installed — videos are unavailable</template>
          </dd>
        </div>
      </dl>

      <p v-if="status && !status.ffmpeg.available" class="callout warning">
        Timelapses and the startrail build video need ffmpeg. Install it with
        <code>sudo apt install ffmpeg</code> and restart the server. Startrails and
        keograms are images and work without it.
      </p>

      <p v-if="status?.pipeline?.dropped" class="callout warning">
        {{ status.pipeline.dropped }} frame(s) were dropped because processing could not
        keep up with the capture interval. Lower the startrail stacking width, or raise
        the interval, if this keeps climbing.
      </p>

      <div v-for="processor in status?.processors || []" :key="processor.name" class="processor">
        <div class="processor-head">
          <label class="check processor-toggle">
            <input
              type="checkbox"
              :checked="drafts[processor.name]?.enabled"
              :disabled="busy === processor.name || !processor.available"
              @change="toggle(processor)"
            />
            <span>
              <strong>{{ processor.label }}</strong>
              <em class="processor-periods">
                {{ processor.session_kinds.join(" · ") }} · {{ processor.category }}
                <template v-if="processor.depends_on.length">
                  · needs {{ processor.depends_on.join(", ") }}
                </template>
              </em>
            </span>
          </label>

          <button
            type="button"
            class="ghost sm"
            @click="expanded = expanded === processor.name ? null : processor.name"
          >
            {{ expanded === processor.name ? "Hide" : "Options" }}
          </button>
        </div>

        <p class="field-hint">{{ processor.description }}</p>

        <p v-if="!processor.available" class="callout warning">
          {{ processor.unavailable_reason }}
        </p>

        <div v-if="expanded === processor.name && drafts[processor.name]" class="processor-body">
          <div class="field-grid">
            <label v-for="field in processor.fields" :key="field.key" class="field">
              <span>
                {{ field.label }}
                <em v-if="field.kind === 'bool'" class="field-unit">on / off</em>
              </span>

              <input
                v-if="field.kind === 'bool'"
                v-model="drafts[processor.name].config[field.key]"
                type="checkbox"
              />
              <select
                v-else-if="field.kind === 'choice'"
                v-model="drafts[processor.name].config[field.key]"
              >
                <option v-for="choice in field.choices" :key="choice" :value="choice">
                  {{ choice }}
                </option>
              </select>
              <input
                v-else-if="field.kind === 'int' || field.kind === 'float'"
                v-model.number="drafts[processor.name].config[field.key]"
                type="number"
                :min="field.minimum ?? undefined"
                :max="field.maximum ?? undefined"
                :step="field.kind === 'float' ? 0.05 : 1"
              />
              <input v-else v-model="drafts[processor.name].config[field.key]" type="text" />
            </label>
          </div>

          <p
            v-for="field in processor.fields.filter((entry) => entry.help_text)"
            :key="`${field.key}-help`"
            class="field-hint"
          >
            <strong>{{ field.label }}:</strong> {{ field.help_text }}
          </p>

          <div class="processor-actions">
            <button
              type="button"
              class="primary sm"
              :disabled="!isDirty(processor) || busy === processor.name"
              @click="save(processor)"
            >
              {{ busy === processor.name ? "Saving…" : "Save" }}
            </button>
            <span v-if="isDirty(processor)" class="badge warning">unsaved</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.processor {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-3);
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--space-3);
}

.processor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.processor-toggle span {
  display: grid;
  gap: 2px;
}

.processor-periods {
  color: var(--text-faint);
  font-size: 11.5px;
  font-style: normal;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.processor-body {
  display: grid;
  gap: var(--space-3);
  border-radius: var(--radius);
  padding: var(--space-3);
  background: var(--surface-inset);
}

.processor-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.retention-grid {
  display: grid;
  gap: var(--space-2);
}

.retention-row {
  display: grid;
  align-items: end;
  gap: var(--space-2);
  grid-template-columns: 1fr 1fr 1fr auto;
}

.retention-name {
  align-self: center;
  font-size: 13px;
  text-transform: capitalize;
}

/* Four columns is unreadable on a phone; stack instead of shrinking. */
@media (max-width: 560px) {
  .retention-row {
    grid-template-columns: 1fr 1fr;
  }

  .retention-name {
    grid-column: 1 / -1;
  }
}
</style>
