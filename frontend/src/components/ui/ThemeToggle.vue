<script setup>
import { computed } from "vue";
import { useTheme } from "../../composables/useTheme";

const { mode, cycleMode } = useTheme();

const LABELS = { dark: "Dark", light: "Light", system: "System" };
const ICONS = { dark: "◗", light: "○", system: "◐" };

const title = computed(() => `Theme: ${LABELS[mode.value]} (click to change)`);
</script>

<template>
  <button
    type="button"
    class="icon ghost"
    :title="title"
    :aria-label="title"
    @click="cycleMode"
  >
    <span aria-hidden="true">{{ ICONS[mode] }}</span>
  </button>
</template>
