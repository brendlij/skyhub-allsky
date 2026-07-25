<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { confirmAction } from "../composables/useConfirm";

const props = defineProps({
  overlays: {
    type: Object,
    required: true
  },
  imageUrl: {
    type: String,
    default: null
  },
  nodeId: {
    type: String,
    default: null
  }
});

const draggingEntity = ref(null);
const dragOffset = ref({ x: 0, y: 0 });
const selectedEntityId = ref(null);
const stage = ref(null);
const previewImage = ref(null);
const previewRect = ref({
  left: 0,
  top: 0,
  width: 1,
  height: 1,
  scale: 1
});
let resizeObserver = null;

// Must match server/app/overlays.py: the burned-in label is only where you
// dragged it if both sides build the box from the same numbers.
const LINE_HEIGHT = 1.2;
const PADDING_RATIO = 0.22;
const MIN_PADDING = 5;

// Snapping, in screen pixels so it feels the same at any preview size.
const SNAP_DISTANCE = 7;
// Matches the inset the presets use, so a hand-placed label can line up with one
// that came from a preset.
const SAFE_MARGIN = 0.02;

const entityElements = new Map();
const guides = ref([]);
let dragContext = null;

function registerEntityElement(entityId, element) {
  if (element) {
    entityElements.set(entityId, element);
  } else {
    entityElements.delete(entityId);
  }
}

function nativePadding(fontSize) {
  return Math.max(MIN_PADDING, Math.floor(fontSize * PADDING_RATIO));
}

function fontSizeOf(entity) {
  return Math.max(8, Number(entity.font_size) || 28);
}

// How far the box extends from its anchor point: 0 = anchor on the leading edge,
// 1 = on the trailing edge. Mirrors anchored_position() on the server.
function anchorFractions(entity) {
  const anchor = entity.anchor || "top-left";

  return {
    x: anchor.includes("right") ? 1 : anchor === "center" ? 0.5 : 0,
    y: anchor.includes("bottom") ? 1 : anchor === "center" ? 0.5 : 0
  };
}

/** Box of an entity in normalised image coordinates, from what is on screen. */
function entityBox(entity) {
  const element = entityElements.get(entity.id);
  const width = element ? element.offsetWidth / previewRect.value.width : 0;
  const height = element ? element.offsetHeight / previewRect.value.height : 0;
  const fraction = anchorFractions(entity);

  return {
    width,
    height,
    left: (Number(entity.x) || 0) - fraction.x * width,
    top: (Number(entity.y) || 0) - fraction.y * height
  };
}

const enabledEntities = computed(() => props.overlays.entities || []);

// Fetched from the server so the picker always matches what actually renders,
// rather than a second hardcoded list that drifts.
const variables = ref([]);
const presets = ref([]);
const hasLiveValues = ref(false);
const variableFilter = ref("");
const insertWithLabels = ref(true);
const textareaRefs = new Map();

// Real values from the node's last capture when available, illustrative samples
// otherwise. Either way this is what the stage preview renders.
const previewValues = computed(() => Object.fromEntries(
  variables.value.map((variable) => [variable.token, variable.value])
));

const knownTokens = computed(() => new Set(variables.value.map((variable) => variable.token)));

const LEGACY_TOKENS = new Set([
  "$node.node_id", "$bme280.temperature_c", "$bme280.humidity_percent",
  "$bme280.pressure_hpa", "$bme280.dew_point_c", "$heater.actual"
]);

function entityWarnings(entity) {
  if (!knownTokens.value.size) return [];

  const matches = String(entity.text || "").match(/\$[A-Za-z][A-Za-z0-9_.]*/g) || [];

  return [...new Set(matches.filter(
    (token) => !knownTokens.value.has(token) && !LEGACY_TOKENS.has(token)
  ))];
}

const allWarnings = computed(() => enabledEntities.value.flatMap(
  (entity) => entityWarnings(entity).map((token) => ({ id: entity.id, token }))
));

const filteredGroups = computed(() => {
  const needle = variableFilter.value.trim().toLowerCase();
  const groups = new Map();

  for (const variable of variables.value) {
    if (needle && !variable.token.toLowerCase().includes(needle)
      && !variable.label.toLowerCase().includes(needle)) {
      continue;
    }

    if (!groups.has(variable.group)) groups.set(variable.group, []);
    groups.get(variable.group).push(variable);
  }

  return [...groups.entries()].map(([group, items]) => ({ group, items }));
});

async function loadVariables() {
  try {
    const query = props.nodeId ? `?node_id=${encodeURIComponent(props.nodeId)}` : "";
    const response = await fetch(`/api/overlays/variables${query}`);
    if (!response.ok) return;

    const data = await response.json();
    variables.value = data.variables;
    presets.value = data.presets || [];
    hasLiveValues.value = Boolean(data.has_live_values);
  } catch {
    // Picker degrades to typing tokens by hand; rendering is unaffected.
  }
}

async function applyPreset(preset) {
  if (!preset) return;

  // A preset replaces every entity, so hand-tuned work would vanish on a stray
  // click without this.
  if (props.overlays.entities.length) {
    const confirmed = await confirmAction({
      title: `Replace ${props.overlays.entities.length} overlay${props.overlays.entities.length === 1 ? "" : "s"}?`,
      message: `"${preset.name}" replaces everything currently on the frame. This is not saved until you press Save.`,
      confirmLabel: "Replace",
      tone: "danger"
    });

    if (!confirmed) return;
  }

  props.overlays.entities.splice(
    0,
    props.overlays.entities.length,
    ...preset.entities.map((entity, index) => ({
      id: `${preset.id}-${index}-${Date.now()}`,
      type: "text",
      label: "Overlay",
      enabled: true,
      color: "#ffffff",
      background: "#000000",
      background_opacity: 0.35,
      ...entity
    }))
  );

  selectedEntityId.value = props.overlays.entities[0]?.id || null;
}

loadVariables();

watch(() => props.nodeId, loadVariables);

function legacyTemplate(entity) {
  if (entity.text) return entity.text;
  if (entity.type === "datetime") return "$capture.datetime";
  if (entity.type === "date") return "$capture.date";
  if (entity.type === "time") return "$capture.time";
  if (entity.type === "period") return "$capture.period";
  if (entity.type === "node_id") return "$node.id";
  return entity.label || "SkyHub";
}

function previewText(entity) {
  return legacyTemplate(entity).replace(
    /\$[A-Za-z][A-Za-z0-9_.]*/g,
    (token) => previewValues.value[token] ?? ""
  );
}

function entityStyle(entity) {
  const nativeFontSize = fontSizeOf(entity);
  const padding = nativePadding(nativeFontSize) * previewRect.value.scale;
  const fontSize = nativeFontSize * previewRect.value.scale;
  const translateX = entity.anchor?.includes("right")
    ? "-100%"
    : entity.anchor === "center"
      ? "-50%"
      : "0";
  const translateY = entity.anchor?.includes("bottom")
    ? "-100%"
    : entity.anchor === "center"
      ? "-50%"
      : "0";

  return {
    left: `${previewRect.value.left + (entity.x || 0) * previewRect.value.width}px`,
    top: `${previewRect.value.top + (entity.y || 0) * previewRect.value.height}px`,
    transform: `translate(${translateX}, ${translateY})`,
    color: entity.color,
    background: hexWithOpacity(entity.background, entity.background_opacity),
    fontSize: `${fontSize}px`,
    lineHeight: LINE_HEIGHT,
    padding: `${padding}px`,
    borderRadius: `${Math.max(4, nativePadding(nativeFontSize)) * previewRect.value.scale}px`
  };
}

function updatePreviewRect() {
  if (!stage.value) return;

  const stageRect = stage.value.getBoundingClientRect();
  const image = previewImage.value;

  if (!image?.naturalWidth || !image?.naturalHeight) {
    previewRect.value = {
      left: 0,
      top: 0,
      width: stageRect.width || 1,
      height: stageRect.height || 1,
      scale: 1
    };
    return;
  }

  const imageAspect = image.naturalWidth / image.naturalHeight;
  const stageAspect = stageRect.width / stageRect.height;
  let width = stageRect.width;
  let height = stageRect.height;
  let left = 0;
  let top = 0;

  if (stageAspect > imageAspect) {
    width = stageRect.height * imageAspect;
    left = (stageRect.width - width) / 2;
  } else {
    height = stageRect.width / imageAspect;
    top = (stageRect.height - height) / 2;
  }

  previewRect.value = {
    left,
    top,
    width,
    height,
    scale: width / image.naturalWidth
  };
}

function hexWithOpacity(hex, opacity) {
  const value = String(hex || "#000000").replace("#", "");
  const red = parseInt(value.slice(0, 2), 16) || 0;
  const green = parseInt(value.slice(2, 4), 16) || 0;
  const blue = parseInt(value.slice(4, 6), 16) || 0;
  return `rgba(${red}, ${green}, ${blue}, ${Number(opacity ?? 0.35)})`;
}

function pointerToImagePosition(event) {
  if (!draggingEntity.value || !stage.value) return;

  const rect = stage.value.getBoundingClientRect();
  const imageRect = previewRect.value;

  if (!imageRect.width || !imageRect.height) return;

  return {
    x: (event.clientX - rect.left - imageRect.left) / imageRect.width,
    y: (event.clientY - rect.top - imageRect.top) / imageRect.height
  };
}

/**
 * Positions worth lining up with, along one axis, in normalised coordinates.
 *
 * The frame's own edges, centre and safe margin, plus the leading edge, centre
 * and trailing edge of every other visible label - so labels line up with each
 * other the way they do in a drawing tool, instead of by eye.
 */
function snapTargets(entity, axis) {
  const size = axis === "x" ? "width" : "height";
  const start = axis === "x" ? "left" : "top";
  const targets = [0, SAFE_MARGIN, 0.5, 1 - SAFE_MARGIN, 1];

  for (const other of enabledEntities.value) {
    if (other.id === entity.id || !other.enabled) continue;

    const box = entityBox(other);

    if (!box[size]) continue;

    targets.push(box[start], box[start] + box[size] / 2, box[start] + box[size]);
  }

  return targets;
}

/**
 * Nudge one axis of the box onto the nearest target.
 *
 * All three of the box's own edges are candidates, so dragging a label near
 * another one's right edge snaps right-to-right as readily as left-to-left.
 */
function snapAxis(entity, axis, boxStart, boxSize) {
  const threshold = SNAP_DISTANCE
    / (axis === "x" ? previewRect.value.width : previewRect.value.height);
  const edges = [boxStart, boxStart + boxSize / 2, boxStart + boxSize];
  let best = null;

  for (const target of snapTargets(entity, axis)) {
    for (const edge of edges) {
      const distance = Math.abs(target - edge);

      if (distance <= threshold && (!best || distance < best.distance)) {
        best = { distance, delta: target - edge, position: target };
      }
    }
  }

  if (!best) return { start: boxStart, guide: null };

  return {
    start: boxStart + best.delta,
    guide: { axis, position: best.position }
  };
}

function updateEntityPosition(event) {
  const entity = draggingEntity.value;

  if (!entity || !dragContext) return;

  const pointer = pointerToImagePosition(event);

  if (!pointer) return;

  const { width, height } = dragContext;
  const fraction = anchorFractions(entity);
  let left = pointer.x - dragOffset.value.x - fraction.x * width;
  let top = pointer.y - dragOffset.value.y - fraction.y * height;
  const active = [];

  // Alt is the usual "place it exactly here" escape hatch.
  if (!event.altKey) {
    const horizontal = snapAxis(entity, "x", left, width);
    const vertical = snapAxis(entity, "y", top, height);

    left = horizontal.start;
    top = vertical.start;

    if (horizontal.guide) active.push(horizontal.guide);
    if (vertical.guide) active.push(vertical.guide);
  }

  guides.value = active;
  applyBox(entity, left, top, width, height);
}

/** Store a box position back as an anchor point, kept fully inside the frame. */
function applyBox(entity, left, top, width, height) {
  const fraction = anchorFractions(entity);
  // The renderer refuses to draw a label half off the frame, so the editor must
  // not pretend otherwise - that mismatch is what made positions "not stick".
  const clampedLeft = Math.min(Math.max(0, 1 - width), Math.max(0, left));
  const clampedTop = Math.min(Math.max(0, 1 - height), Math.max(0, top));

  entity.x = clampedLeft + fraction.x * width;
  entity.y = clampedTop + fraction.y * height;
}

function startDrag(entity, event) {
  const box = entityBox(entity);

  draggingEntity.value = entity;
  dragContext = { width: box.width, height: box.height };

  const pointer = pointerToImagePosition(event);

  dragOffset.value = pointer
    ? {
        x: pointer.x - (Number(entity.x) || 0),
        y: pointer.y - (Number(entity.y) || 0)
      }
    : { x: 0, y: 0 };
  event.currentTarget?.setPointerCapture?.(event.pointerId);
  window.addEventListener("pointermove", updateEntityPosition);
  window.addEventListener("pointerup", stopDrag, { once: true });
}

function stopDrag() {
  window.removeEventListener("pointermove", updateEntityPosition);
  draggingEntity.value = null;
  dragContext = null;
  dragOffset.value = { x: 0, y: 0 };
  guides.value = [];
}

/** Arrow keys move by whole image pixels - snapping cannot reach every position. */
function nudgeEntity(entity, deltaX, deltaY, event) {
  const step = event.shiftKey ? 10 : 1;
  const image = previewImage.value;
  const imageWidth = image?.naturalWidth || 1;
  const imageHeight = image?.naturalHeight || 1;
  const box = entityBox(entity);

  applyBox(
    entity,
    box.left + (deltaX * step) / imageWidth,
    box.top + (deltaY * step) / imageHeight,
    box.width,
    box.height
  );
}

/** Guide overlay geometry, in stage pixels. */
function guideStyle(guide) {
  const rect = previewRect.value;

  if (guide.axis === "x") {
    return {
      left: `${rect.left + guide.position * rect.width}px`,
      top: `${rect.top}px`,
      height: `${rect.height}px`,
      width: "1px"
    };
  }

  return {
    left: `${rect.left}px`,
    top: `${rect.top + guide.position * rect.height}px`,
    width: `${rect.width}px`,
    height: "1px"
  };
}

function addTextEntity() {
  const entity = {
    id: `text-${Date.now()}`,
    type: "text",
    label: "Overlay",
    enabled: true,
    x: 0.5,
    y: 0.5,
    anchor: "center",
    font_size: 28,
    color: "#ffffff",
    background: "#000000",
    background_opacity: 0.35,
    text: "$capture.datetime"
  };

  props.overlays.entities.push(entity);
  selectedEntityId.value = entity.id;
}

function removeEntity(entityId) {
  const index = props.overlays.entities.findIndex((entity) => entity.id === entityId);

  if (index >= 0) {
    props.overlays.entities.splice(index, 1);
  }

  if (selectedEntityId.value === entityId) {
    selectedEntityId.value = props.overlays.entities[0]?.id || null;
  }
}

function selectEntity(entity) {
  selectedEntityId.value = entity.id;
  entity.type = "text";
  entity.text = legacyTemplate(entity);
}

function selectedEntity() {
  return props.overlays.entities.find((entity) => entity.id === selectedEntityId.value) || props.overlays.entities[0];
}

function registerTextarea(entityId, element) {
  if (element) {
    textareaRefs.set(entityId, element);
  } else {
    textareaRefs.delete(entityId);
  }
}

function insertVariable(variable) {
  let entity = selectedEntity();

  if (!entity) {
    addTextEntity();
    entity = selectedEntity();
  }

  if (!entity) return;

  selectEntity(entity);

  const fragment = insertWithLabels.value ? variable.snippet : variable.token;
  const textarea = textareaRefs.get(entity.id);
  const current = entity.text || "";

  // Insert at the caret rather than appending, so a variable can go before or
  // inside text that is already written.
  if (!textarea || textarea.selectionStart === null) {
    entity.text = [current, fragment].filter(Boolean).join(" ");
    return;
  }

  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const needsLeadingSpace = start > 0 && !/\s$/.test(current.slice(0, start));
  const insertion = (needsLeadingSpace ? " " : "") + fragment;

  entity.text = current.slice(0, start) + insertion + current.slice(end);

  const caret = start + insertion.length;
  requestAnimationFrame(() => {
    textarea.focus();
    textarea.setSelectionRange(caret, caret);
  });
}

function normalizeEntities() {
  for (const entity of props.overlays.entities || []) {
    entity.type = "text";
    entity.text = legacyTemplate(entity);
    entity.label = entity.label || "Overlay";
  }

  if (!selectedEntityId.value && props.overlays.entities?.length) {
    selectedEntityId.value = props.overlays.entities[0].id;
  }
}

onMounted(() => {
  normalizeEntities();
  updatePreviewRect();

  if (stage.value && "ResizeObserver" in window) {
    resizeObserver = new ResizeObserver(updatePreviewRect);
    resizeObserver.observe(stage.value);
  } else {
    window.addEventListener("resize", updatePreviewRect);
  }
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  window.removeEventListener("resize", updatePreviewRect);
  window.removeEventListener("pointermove", updateEntityPosition);
});

watch(() => props.imageUrl, () => {
  requestAnimationFrame(updatePreviewRect);
});

watch(() => props.overlays.entities, normalizeEntities, { immediate: true });

/** Keep every label inside the frame after an edit that changed its box size. */
function reclampEntities() {
  if (draggingEntity.value) return;

  for (const entity of props.overlays.entities || []) {
    const box = entityBox(entity);

    if (!box.width || !box.height) continue;

    applyBox(entity, box.left, box.top, box.width, box.height);
  }
}

watch(
  () => (props.overlays.entities || [])
    .map((entity) => `${entity.text}|${entity.font_size}|${entity.anchor}`)
    .join(" "),
  () => requestAnimationFrame(reclampEntities)
);
</script>

<template>
  <div class="overlay-layout">
    <section class="panel">
      <div class="panel-header">
        <h2>
          Preview
          <span v-if="!imageUrl" class="panel-title-note">no capture yet</span>
        </h2>
        <span class="badge" :class="hasLiveValues ? 'success' : ''">
          {{ hasLiveValues ? "live values" : "sample values" }}
        </span>
      </div>

      <div class="panel-body">
        <div ref="stage" class="overlay-stage">
          <img v-if="imageUrl" ref="previewImage" :src="imageUrl" alt="" @load="updatePreviewRect" />
          <div v-else class="overlay-placeholder">
            The preview uses this node's most recent capture once one exists.
          </div>
          <button
            v-for="entity in enabledEntities"
            v-show="entity.enabled"
            :key="entity.id"
            :ref="(element) => registerEntityElement(entity.id, element)"
            class="overlay-entity"
            :class="{ selected: entity.id === selectedEntityId }"
            type="button"
            :style="entityStyle(entity)"
            :aria-label="`Move ${entity.label || 'overlay'}`"
            @pointerdown.prevent="selectEntity(entity); startDrag(entity, $event)"
            @keydown.left.prevent="nudgeEntity(entity, -1, 0, $event)"
            @keydown.right.prevent="nudgeEntity(entity, 1, 0, $event)"
            @keydown.up.prevent="nudgeEntity(entity, 0, -1, $event)"
            @keydown.down.prevent="nudgeEntity(entity, 0, 1, $event)"
          >
            {{ previewText(entity) }}
          </button>
          <span
            v-for="guide in guides"
            :key="`${guide.axis}-${guide.position}`"
            class="overlay-guide"
            :style="guideStyle(guide)"
            aria-hidden="true"
          />
        </div>
        <p class="field-hint">
          Drag a label to reposition it - it snaps to the frame and to the other labels.
          Hold Alt to place it freely, or nudge the selected label with the arrow keys.
        </p>
      </div>
    </section>

    <div class="stack">
      <section class="panel">
        <div class="panel-header">
          <h2>Overlays</h2>
          <label class="check">
            <input v-model="overlays.enabled" type="checkbox" />
            Enabled
          </label>
        </div>

        <div class="panel-body">
          <div class="row wrap">
            <select
              v-if="presets.length"
              class="grow"
              @change="applyPreset(presets[$event.target.selectedIndex - 1]); $event.target.selectedIndex = 0"
            >
              <option value="">Start from a preset…</option>
              <option v-for="preset in presets" :key="preset.id" :title="preset.description">
                {{ preset.name }}
              </option>
            </select>
            <button type="button" class="primary" @click="addTextEntity">Add label</button>
          </div>

          <p v-if="allWarnings.length" class="callout warning">
            <span>
              Unknown variable{{ allWarnings.length > 1 ? "s" : "" }} render as empty text:
              <code v-for="warning in allWarnings" :key="warning.id + warning.token">{{ warning.token }}</code>
            </span>
          </p>

          <div v-if="!overlays.entities.length" class="empty-state">
            <span class="empty-icon" aria-hidden="true">◫</span>
            <strong>No labels yet</strong>
            <p>Pick a preset for a ready-made four-corner layout, or add a label and build it up.</p>
          </div>

          <div v-else class="entity-list">
            <article
              v-for="entity in overlays.entities"
              :key="entity.id"
              class="entity-row"
              :class="{ selected: entity.id === selectedEntityId }"
              @focusin="selectEntity(entity)"
              @click="selectEntity(entity)"
            >
              <div class="entity-row-head">
                <input v-model="entity.enabled" type="checkbox" :aria-label="`Show ${entity.label}`" />
                <input
                  v-model="entity.label"
                  class="grow"
                  aria-label="Label name"
                  @focus="selectEntity(entity)"
                />
                <button
                  type="button"
                  class="icon ghost"
                  :aria-label="`Remove ${entity.label}`"
                  title="Remove"
                  @click="removeEntity(entity.id)"
                >
                  ×
                </button>
              </div>

              <!-- Only the selected label expands. With three or four labels the
                   full form on every one pushed the variable picker off screen. -->
              <div v-if="entity.id !== selectedEntityId" class="entity-preview">
                {{ previewText(entity) || "(renders empty)" }}
              </div>

              <template v-else>
                <label class="field">
                  <span>Template</span>
                  <textarea
                    :ref="(element) => registerTextarea(entity.id, element)"
                    v-model="entity.text"
                    rows="2"
                    :class="{ invalid: entityWarnings(entity).length }"
                    placeholder="$capture.datetime"
                    @focus="selectEntity(entity)"
                  />
                </label>
                <div class="entity-preview">{{ previewText(entity) || "(renders empty)" }}</div>

                <div class="field-grid">
                <label class="field">
                  <span>Anchor</span>
                  <select v-model="entity.anchor">
                    <option value="top-left">Top left</option>
                    <option value="top-right">Top right</option>
                    <option value="bottom-left">Bottom left</option>
                    <option value="bottom-right">Bottom right</option>
                    <option value="center">Centre</option>
                  </select>
                </label>
                <label class="field">
                  <span>Font size</span>
                  <input v-model.number="entity.font_size" type="number" min="8" max="160" />
                </label>
                <label class="field">
                  <span>Text</span>
                  <input v-model="entity.color" type="color" />
                </label>
                <label class="field">
                  <span>Background</span>
                  <input v-model="entity.background" type="color" />
                </label>
                <label class="field">
                  <span>Opacity <em class="field-value">{{ Number(entity.background_opacity ?? 0.35).toFixed(2) }}</em></span>
                  <input v-model.number="entity.background_opacity" type="range" min="0" max="1" step="0.05" />
                </label>
                </div>
              </template>
            </article>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <h2>
            Variables
            <span class="panel-title-note">{{ variables.length }} available</span>
          </h2>
          <label class="check">
            <input v-model="insertWithLabels" type="checkbox" />
            With labels
          </label>
        </div>

        <div class="panel-body">
          <div class="overlay-variable-picker">
            <div class="overlay-variable-search">
              <input
                v-model="variableFilter"
                type="search"
                placeholder="Filter — exposure, gain, moon…"
                aria-label="Filter variables"
              />
            </div>

            <div v-for="entry in filteredGroups" :key="entry.group" class="overlay-variable-group">
              <h4>{{ entry.group }}</h4>
              <div class="overlay-variables">
                <button
                  v-for="variable in entry.items"
                  :key="variable.token"
                  type="button"
                  :title="`${variable.snippet}  →  ${variable.value}`"
                  @click="insertVariable(variable)"
                >
                  {{ variable.label }}
                  <em>{{ variable.value }}</em>
                </button>
              </div>
            </div>

            <p v-if="!filteredGroups.length" class="muted">No variables match that filter.</p>
          </div>
          <p class="field-hint">Clicking inserts at the cursor in the selected label's template.</p>
        </div>
      </section>
    </div>
  </div>
</template>
