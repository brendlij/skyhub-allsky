<script setup>
import { RouterLink } from "vue-router";
import { useSkyHub } from "../../composables/useSkyHub";
import { useTheme } from "../../composables/useTheme";
import ThemeToggle from "../ui/ThemeToggle.vue";

const { nodes } = useSkyHub();
const { sidebarCollapsed, toggleSidebar } = useTheme();

const links = [
  { to: "/monitor", label: "Monitor", icon: "◉" },
  { to: "/captures", label: "Captures", icon: "▦" },
  { to: "/overlays", label: "Overlays", icon: "◫" },
  { to: "/settings", label: "Settings", icon: "⚙" },
  { to: "/nodes", label: "Nodes", icon: "⬡" }
];
</script>

<template>
  <aside class="sidebar">
    <RouterLink class="sidebar-brand" to="/monitor">
      <span class="sidebar-mark" aria-hidden="true">S</span>
      <span class="sidebar-brand-text">
        <strong>SkyHub</strong>
        <small>Allsky console</small>
      </span>
    </RouterLink>

    <nav class="sidebar-nav" aria-label="Main navigation">
      <RouterLink v-for="link in links" :key="link.to" :to="link.to" :title="link.label">
        <span class="nav-icon" aria-hidden="true">{{ link.icon }}</span>
        <span>{{ link.label }}</span>
        <span v-if="link.to === '/nodes' && nodes.length" class="badge nav-badge">
          {{ nodes.filter((node) => node.online).length }}/{{ nodes.length }}
        </span>
      </RouterLink>
    </nav>

    <div class="sidebar-footer">
      <div class="sidebar-footer-row">
        <ThemeToggle />
        <button
          type="button"
          class="icon ghost"
          :title="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          :aria-label="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
          @click="toggleSidebar"
        >
          <span aria-hidden="true">{{ sidebarCollapsed ? "»" : "«" }}</span>
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar-footer-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

/* Collapsed rail stacks the two controls so neither overflows the 68px width. */
:global(.shell.collapsed) .sidebar-footer-row {
  flex-direction: column;
}

@media (max-width: 1000px) {
  .sidebar-footer-row {
    flex-direction: column;
  }
}
</style>
