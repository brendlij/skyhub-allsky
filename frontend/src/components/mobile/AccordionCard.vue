<script setup>
import { ref } from "vue";

/* Collapsed it shows the one number worth knowing; expanded it shows the rest.
 *
 * The open/close animation runs on grid-template-rows rather than height, which
 * is what lets it animate to the content's real size without measuring it in
 * JavaScript on every frame.
 */

const props = defineProps({
  title: { type: String, required: true },
  summary: { type: String, default: "" },
  tone: { type: String, default: "" },
  defaultOpen: { type: Boolean, default: false }
});

const open = ref(props.defaultOpen);
</script>

<template>
  <section class="accordion" :class="{ open }">
    <button
      type="button"
      class="accordion-head"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="accordion-title">{{ title }}</span>
      <span v-if="summary" class="accordion-summary" :class="tone">{{ summary }}</span>
      <span class="accordion-chevron" aria-hidden="true">⌄</span>
    </button>

    <div class="accordion-shutter">
      <div class="accordion-inner">
        <slot />
      </div>
    </div>
  </section>
</template>

<style scoped>
.accordion {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.accordion-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  min-height: var(--tap-target);
  border: 0;
  border-radius: var(--radius-card);
  padding: var(--space-4) var(--space-5);
  background: transparent;
  text-align: left;
}

.accordion-head:hover:not(:disabled) {
  border-color: transparent;
  background: transparent;
}

.accordion-title {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  font-weight: 600;
}

.accordion-summary {
  color: var(--text-muted);
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  font-weight: 550;
}

.accordion-summary.success { color: var(--success); }
.accordion-summary.warning { color: var(--warning); }
.accordion-summary.danger { color: var(--danger); }

.accordion-chevron {
  flex: none;
  color: var(--text-faint);
  font-size: 15px;
  line-height: 1;
  transition: transform var(--motion);
}

.accordion.open .accordion-chevron {
  transform: rotate(180deg);
}

.accordion-shutter {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--motion);
}

.accordion.open .accordion-shutter {
  grid-template-rows: 1fr;
}

.accordion-inner {
  overflow: hidden;
}

.accordion.open .accordion-inner {
  /* Padding only once open, so a collapsed card is exactly its header. */
  padding: 0 var(--space-5) var(--space-5);
}

@media (prefers-reduced-motion: reduce) {
  .accordion-shutter {
    transition: none;
  }
}
</style>
