import { computed, ref, watch } from "vue";

const STORAGE_KEY = "skyhub.theme";
const SIDEBAR_KEY = "skyhub.sidebar";

// "system" follows the OS; "dark"/"light" stamp data-theme on <html> and win over
// the media query in both directions.
const MODES = ["dark", "light", "system"];

function readStored(key, fallback, allowed) {
  try {
    const value = window.localStorage.getItem(key);
    return allowed ? (allowed.includes(value) ? value : fallback) : (value ?? fallback);
  } catch {
    return fallback;
  }
}

function persist(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Private mode or blocked storage: the choice just does not survive a reload.
  }
}

const mode = ref(readStored(STORAGE_KEY, "dark", MODES));
const sidebarCollapsed = ref(readStored(SIDEBAR_KEY, "false") === "true");

const systemPrefersDark = ref(true);

if (typeof window !== "undefined" && window.matchMedia) {
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  systemPrefersDark.value = query.matches;
  query.addEventListener("change", (event) => {
    systemPrefersDark.value = event.matches;
  });
}

const resolvedTheme = computed(() => {
  if (mode.value === "system") return systemPrefersDark.value ? "dark" : "light";
  return mode.value;
});

function apply() {
  const root = document.documentElement;

  if (mode.value === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", mode.value);
  }
}

watch(mode, (value) => {
  persist(STORAGE_KEY, value);
  apply();
}, { immediate: true });

watch(sidebarCollapsed, (value) => persist(SIDEBAR_KEY, String(value)));

function setMode(next) {
  if (MODES.includes(next)) mode.value = next;
}

function cycleMode() {
  mode.value = MODES[(MODES.indexOf(mode.value) + 1) % MODES.length];
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
}

export function useTheme() {
  return {
    mode,
    modes: MODES,
    resolvedTheme,
    setMode,
    cycleMode,
    sidebarCollapsed,
    toggleSidebar
  };
}
