<script setup>
defineProps({
  icon: { type: String, default: "○" },
  title: { type: String, required: true },
  message: { type: String, default: "" }
});
</script>

<template>
  <div class="empty-state">
    <span class="empty-icon" aria-hidden="true">{{ icon }}</span>
    <strong>{{ title }}</strong>
    <p v-if="message">{{ message }}</p>
    <slot />
  </div>
</template>
