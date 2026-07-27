<script setup>
/* The mobile grouping primitive: a rounded card with an optional title row.
 * Cards replace the desktop panel because on a phone a bordered box that spans
 * the viewport reads as a section, not as a container. */

defineProps({
  title: { type: String, default: "" },
  note: { type: String, default: "" },
  flush: { type: Boolean, default: false }
});
</script>

<template>
  <section class="mobile-card" :class="{ flush }">
    <header v-if="title || $slots.action" class="mobile-card-head">
      <div class="mobile-card-title">
        <h2>{{ title }}</h2>
        <small v-if="note">{{ note }}</small>
      </div>
      <slot name="action" />
    </header>

    <slot />
  </section>
</template>

<style scoped>
.mobile-card {
  display: grid;
  align-content: start;
  gap: var(--space-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  padding: var(--space-5);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.mobile-card.flush {
  padding: 0;
  overflow: hidden;
}

.mobile-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: 24px;
}

.mobile-card.flush .mobile-card-head {
  padding: var(--space-5) var(--space-5) 0;
}

.mobile-card-title {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.mobile-card-title h2 {
  font-size: 15px;
  font-weight: 600;
}

.mobile-card-title small {
  color: var(--text-faint);
  font-size: 12.5px;
}
</style>
