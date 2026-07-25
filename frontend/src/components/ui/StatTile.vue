<script setup>
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: "-" },
  detail: { type: String, default: "" },
  tone: { type: String, default: "" },
  icon: { type: String, default: "" },
  loading: { type: Boolean, default: false }
});
</script>

<template>
  <div class="stat-tile" :class="tone">
    <div class="stat-head">
      <span class="stat-label">
        <span v-if="icon" class="stat-icon" aria-hidden="true">{{ icon }}</span>
        {{ label }}
      </span>
      <slot name="action" />
    </div>
    <div v-if="loading" class="skeleton stat-skeleton" />
    <strong v-else class="stat-value">{{ value }}</strong>
    <small v-if="detail" class="stat-detail">{{ detail }}</small>
    <slot />
  </div>
</template>

<style scoped>
.stat-tile {
  display: grid;
  align-content: start;
  gap: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-4);
  background: var(--surface);
}

.stat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  min-height: 20px;
}

.stat-label {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-faint);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.stat-icon {
  font-size: 12px;
  letter-spacing: 0;
}

.stat-value {
  font-family: var(--font-mono);
  font-size: 19px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.15;
  overflow-wrap: anywhere;
}

.stat-detail {
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.4;
}

.stat-skeleton {
  width: 60%;
  height: 22px;
}

.stat-tile.success .stat-value { color: var(--success); }
.stat-tile.warning .stat-value { color: var(--warning); }
.stat-tile.danger .stat-value { color: var(--danger); }
.stat-tile.accent .stat-value { color: var(--accent); }
</style>
