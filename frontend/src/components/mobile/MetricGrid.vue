<script setup>
/* Two columns of label-over-value. The value carries the weight and the label
 * stays quiet, so a screen full of numbers can be read at a glance rather than
 * decoded row by row.
 *
 * Items: { label, value, tone?, mono?, hint? }. Falsy values are dropped by the
 * caller, not here, so an empty grid never renders a row of dashes.
 */

defineProps({
  items: { type: Array, required: true },
  columns: { type: Number, default: 2 }
});
</script>

<template>
  <dl class="metric-grid" :style="{ '--metric-columns': columns }">
    <div v-for="item in items" :key="item.label" class="metric">
      <dt>{{ item.label }}</dt>
      <dd :class="[item.tone, { mono: item.mono }]">{{ item.value }}</dd>
      <small v-if="item.hint">{{ item.hint }}</small>
    </div>
  </dl>
</template>

<style scoped>
.metric-grid {
  display: grid;
  gap: var(--space-4) var(--space-3);
  grid-template-columns: repeat(var(--metric-columns), minmax(0, 1fr));
}

.metric {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.metric dt {
  color: var(--text-muted);
  font-size: 12.5px;
  font-weight: 450;
}

.metric dd {
  margin: 0;
  color: var(--text);
  font-size: 17px;
  font-variant-numeric: tabular-nums;
  font-weight: 550;
  letter-spacing: -0.015em;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.metric dd.mono {
  font-family: var(--font-mono);
  font-size: 15px;
}

.metric dd.success { color: var(--success); }
.metric dd.warning { color: var(--warning); }
.metric dd.danger { color: var(--danger); }
.metric dd.faint { color: var(--text-faint); }

.metric small {
  color: var(--text-faint);
  font-size: 12px;
}
</style>
