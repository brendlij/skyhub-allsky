<script setup>
import { RouterLink } from "vue-router";
import { useSkyHub } from "../../composables/useSkyHub";

/* The mobile equivalent of the sidebar. Fixed to the bottom because that is the
 * half of a phone a thumb reaches without regripping. */

const { nodes } = useSkyHub();

const links = [
  { to: "/monitor", label: "Monitor", icon: "◉" },
  { to: "/captures", label: "Captures", icon: "▦" },
  { to: "/overlays", label: "Overlays", icon: "◫" },
  { to: "/settings", label: "Settings", icon: "⚙" },
  { to: "/nodes", label: "Nodes", icon: "⬡" }
];

const offline = () => nodes.value.length > 0 && nodes.value.every((node) => !node.online);
</script>

<template>
  <nav class="bottom-nav" aria-label="Main navigation">
    <RouterLink
      v-for="link in links"
      :key="link.to"
      class="bottom-nav-item"
      :to="link.to"
    >
      <span class="bottom-nav-icon" aria-hidden="true">
        {{ link.icon }}
        <span v-if="link.to === '/nodes' && offline()" class="bottom-nav-dot" />
      </span>
      <span class="bottom-nav-label">{{ link.label }}</span>
    </RouterLink>
  </nav>
</template>

<style scoped>
.bottom-nav {
  position: fixed;
  z-index: 60;
  right: 0;
  bottom: 0;
  left: 0;
  display: grid;
  grid-auto-columns: 1fr;
  grid-auto-flow: column;
  height: var(--bottom-nav-total);
  padding-bottom: var(--safe-bottom);
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 80%, transparent);
  backdrop-filter: blur(20px) saturate(180%);
}

.bottom-nav-item {
  display: grid;
  align-content: center;
  justify-items: center;
  gap: 3px;
  min-height: var(--tap-target);
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 500;
  text-decoration: none;
  -webkit-tap-highlight-color: transparent;
  transition: color var(--motion-fast);
}

.bottom-nav-item:hover {
  text-decoration: none;
}

.bottom-nav-item.router-link-active {
  color: var(--accent);
}

.bottom-nav-icon {
  position: relative;
  font-size: 19px;
  line-height: 1;
  transition: transform var(--motion-fast);
}

/* A press that moves is a press that registered, without waiting for the route. */
.bottom-nav-item:active .bottom-nav-icon {
  transform: scale(0.88);
}

.bottom-nav-dot {
  position: absolute;
  top: -2px;
  right: -5px;
  width: 7px;
  height: 7px;
  border-radius: var(--radius-full);
  background: var(--danger);
}

.bottom-nav-label {
  letter-spacing: 0.01em;
}
</style>
