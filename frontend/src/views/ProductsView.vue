<script setup>
import { onMounted } from "vue";
import EmptyState from "../components/ui/EmptyState.vue";
import { formatBytes } from "../api/skyhub";
import {
  isVideo,
  productDisplayUrl,
  productLabel,
  productUrl,
  useProcessing
} from "../composables/useProcessing";
import { useSkyHub } from "../composables/useSkyHub";
import { useToasts } from "../composables/useToasts";

const {
  byPeriod,
  dates,
  live,
  loading,
  progressFor,
  selectedDate,
  sessions,
  closeSession,
  refresh,
  selectDate
} = useProcessing();

const STAGE_LABELS = {
  idle: "Idle",
  running: "Running",
  encoding: "Encoding",
  finalising: "Finalising",
  completed: "Completed",
  failed: "Failed"
};

function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage;
}

/** Progress entries for a session, as a sorted list the template can iterate. */
function progressRows(session) {
  return Object.entries(progressFor(session))
    .map(([processor, state]) => ({ processor, ...state }))
    .sort((a, b) => a.processor.localeCompare(b.processor));
}

const { selectedNodeId } = useSkyHub();
const { notify, notifyError } = useToasts();

onMounted(refresh);

const openSessions = () => sessions.value.filter((session) => session.status === "open");

function formatDuration(seconds) {
  if (!seconds) return "";

  const whole = Math.round(seconds);

  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}m ${String(whole % 60).padStart(2, "0")}s`;
}

async function finaliseNow(session) {
  try {
    const result = await closeSession(session);

    notify(
      result.products?.length
        ? `Finalised ${session.period}: ${result.products.join(", ")}`
        : `Finalised ${session.period} — nothing to produce`
    );
  } catch (error) {
    notifyError(error);
  }
}
</script>

<template>
  <div class="stack-lg">
    <div class="page-head">
      <div class="page-head-text">
        <h1>Products</h1>
        <p>Startrails, keograms and timelapses built from this node's captures</p>
      </div>

      <div class="page-head-actions">
        <label v-if="dates.length" class="field inline-field">
          <span class="visually-hidden">Date</span>
          <select :value="selectedDate" @change="selectDate($event.target.value)">
            <option v-for="date in dates" :key="date" :value="date">{{ date }}</option>
          </select>
        </label>
        <button type="button" class="sm" :disabled="loading" @click="refresh">
          {{ loading ? "Loading…" : "Refresh" }}
        </button>
      </div>
    </div>

    <!-- Tonight, while it is still happening. -->
    <section v-if="live.length" class="panel">
      <div class="panel-header">
        <h2>
          Live
          <span class="badge success">building now</span>
        </h2>
      </div>
      <div class="panel-body">
        <p class="field-hint">
          These update after every capture. The finished versions are written when the
          session ends at sunrise or sunset — no reprocessing.
        </p>

        <div class="product-grid">
          <figure v-for="product in live" :key="product.product_id" class="product-card">
            <a :href="productUrl(product)" target="_blank" rel="noopener">
              <img :src="productDisplayUrl(product)" :alt="productLabel(product.kind)" loading="lazy" />
            </a>
            <figcaption>
              <strong>{{ productLabel(product.kind) }}</strong>
              <span class="muted">
                {{ product.frame_count }} frames · {{ product.width }}×{{ product.height }}
              </span>
            </figcaption>
          </figure>
        </div>
      </div>
    </section>

    <!-- Sessions still open, with a manual finalise. -->
    <section v-if="openSessions().length" class="panel">
      <div class="panel-header">
        <h2>Open sessions</h2>
      </div>
      <div class="panel-body">
        <ul class="session-rows">
          <li v-for="session in openSessions()" :key="session.session_key" class="session-row">
            <div class="session-row-main">
              <strong>
                {{ session.archive_date }} · {{ session.period }}
                <span v-if="session.session_kind !== 'solar'" class="badge">{{ session.label || session.session_kind }}</span>
              </strong>
              <span class="muted">{{ session.frame_count }} frames collected</span>

              <!-- Live progress per processor. Pushed over the WebSocket, so this
                   moves during an encode without the page polling anything. -->
              <ul v-if="progressRows(session).length" class="progress-rows">
                <li v-for="row in progressRows(session)" :key="row.processor">
                  <span class="progress-name">{{ row.processor }}</span>
                  <span class="progress-stage" :class="row.stage">{{ stageLabel(row.stage) }}</span>
                  <span v-if="row.percent != null" class="meter progress-meter">
                    <span class="meter-fill" :style="{ width: `${row.percent}%` }" />
                  </span>
                  <span v-if="row.detail" class="muted progress-detail">{{ row.detail }}</span>
                </li>
              </ul>
            </div>
            <button type="button" class="sm" @click="finaliseNow(session)">Finalise now</button>
          </li>
        </ul>
        <p class="field-hint">
          A session closes on its own when the sun crosses. Finalise early to encode the
          videos now — useful after changing a setting, rather than waiting a night to see it.
        </p>
      </div>
    </section>

    <!-- Everything finished, grouped by period. -->
    <section v-for="period in ['night', 'day']" :key="period" class="panel">
      <div v-if="byPeriod[period]?.length" class="panel-header">
        <h2>{{ period === "night" ? "Night" : "Day" }} <span class="muted">{{ selectedDate }}</span></h2>
      </div>

      <div v-if="byPeriod[period]?.length" class="panel-body">
        <div class="product-grid">
          <figure v-for="product in byPeriod[period]" :key="product.product_id" class="product-card">
            <template v-if="product.state === 'failed'">
              <div class="product-failed">
                <strong>{{ productLabel(product.kind) }}</strong>
                <span>{{ product.metadata?.error || "Could not be produced" }}</span>
              </div>
            </template>

            <template v-else-if="isVideo(product)">
              <video :src="productUrl(product)" controls preload="metadata" playsinline />
            </template>

            <!-- The <img> gets the web-sized variant; the link goes to the
                 full-resolution original. -->
            <a v-else :href="productUrl(product)" target="_blank" rel="noopener">
              <img :src="productDisplayUrl(product)" :alt="productLabel(product.kind)" loading="lazy" />
            </a>

            <figcaption v-if="product.state !== 'failed'">
              <strong>{{ productLabel(product.kind) }}</strong>
              <span class="muted">
                {{ product.frame_count }} frames
                <template v-if="product.duration_seconds"> · {{ formatDuration(product.duration_seconds) }}</template>
                · {{ formatBytes(product.size_bytes) }}
                <template v-if="product.version > 1"> · v{{ product.version }}</template>
              </span>
            </figcaption>
          </figure>
        </div>
      </div>
    </section>

    <div v-if="!live.length && !byPeriod.night?.length && !byPeriod.day?.length" class="panel">
      <EmptyState
        icon="✦"
        title="Nothing produced yet"
        :message="selectedNodeId
          ? 'Products appear as captures arrive. The first startrail column lands with the first night frame.'
          : 'Pick a node in the top bar.'"
      />
    </div>
  </div>
</template>

<style scoped>
.page-head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.inline-field {
  width: auto;
}

.product-grid {
  display: grid;
  gap: var(--space-4);
  grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
}

.product-card {
  display: grid;
  gap: var(--space-2);
  margin: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: var(--space-2);
  background: var(--surface-inset);
}

/* A keogram is extremely wide and a startrail is 4:3. Letting each keep its own
 * shape inside a fixed box beats cropping either one to a common tile. */
.product-card img,
.product-card video {
  display: block;
  width: 100%;
  max-height: 420px;
  border-radius: var(--radius);
  background: var(--image-backdrop);
  object-fit: contain;
}

.product-card figcaption {
  display: grid;
  gap: 2px;
  padding: 0 var(--space-1) var(--space-1);
  font-size: 13px;
}

.product-card figcaption .muted {
  font-size: 12px;
}

.product-failed {
  display: grid;
  gap: var(--space-1);
  border-radius: var(--radius);
  padding: var(--space-4);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 13px;
}

.session-rows {
  display: grid;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.session-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  padding: var(--space-2) var(--space-3);
  background: var(--surface-inset);
}

.session-row-main {
  display: grid;
  flex: 1;
  gap: 2px;
  font-size: 13.5px;
}

.session-row-main .muted {
  font-size: 12px;
}

.progress-rows {
  display: grid;
  gap: var(--space-1);
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}

.progress-rows li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 12px;
}

.progress-name {
  min-width: 118px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.progress-stage {
  min-width: 74px;
  color: var(--text-faint);
}

/* Only the stages that mean something unusual get a colour; "running" is the
   normal state and colouring it would make the list read as all alerts. */
.progress-stage.encoding { color: var(--accent); }
.progress-stage.finalising { color: var(--accent); }
.progress-stage.completed { color: var(--success); }
.progress-stage.failed { color: var(--danger); }

.progress-meter {
  flex: 1;
  max-width: 160px;
}

.progress-detail {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
