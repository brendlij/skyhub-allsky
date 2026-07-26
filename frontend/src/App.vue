<script setup>
import { computed, ref } from "vue";
import { RouterView } from "vue-router";
import AppSidebar from "./components/layout/AppSidebar.vue";
import AppTopbar from "./components/layout/AppTopbar.vue";
import BottomNav from "./components/mobile/BottomNav.vue";
import MobileHeader from "./components/mobile/MobileHeader.vue";
import ApiKeyDialog from "./components/ui/ApiKeyDialog.vue";
import ConfirmDialog from "./components/ui/ConfirmDialog.vue";
import ToastStack from "./components/ui/ToastStack.vue";
import { useSkyHub } from "./composables/useSkyHub";
import { usePullToRefresh } from "./composables/usePullToRefresh";
import { useTheme } from "./composables/useTheme";
import { useViewport } from "./composables/useViewport";

/* Two shells, one application.
 *
 * Below 1024px the sidebar and top bar are replaced by an app bar and a bottom
 * navigation; above it, the desktop layout is untouched. The views themselves
 * are shared - they adapt inside, rather than being forked per platform.
 */

const { sidebarCollapsed } = useTheme();
const { isMobile } = useViewport();
const { refreshDashboard } = useSkyHub();

const workspace = ref(null);
const { distance, refreshing, threshold } = usePullToRefresh(
  workspace,
  refreshDashboard,
  { enabled: isMobile }
);

const pullStyle = computed(() => ({
  transform: `translate3d(-50%, ${distance.value}px, 0) rotate(${distance.value * 3}deg)`,
  opacity: String(Math.min(1, distance.value / threshold))
}));
</script>

<template>
  <div v-if="isMobile" class="shell-mobile">
    <MobileHeader />

    <main ref="workspace" class="workspace-mobile">
      <div class="pull-indicator" :class="{ active: refreshing }" :style="pullStyle" aria-hidden="true">
        ↻
      </div>

      <RouterView v-slot="{ Component }">
        <Transition name="page" mode="out-in">
          <component :is="Component" />
        </Transition>
      </RouterView>
    </main>

    <BottomNav />

    <ToastStack />
    <ConfirmDialog />
    <ApiKeyDialog />
  </div>

  <div v-else class="shell" :class="{ collapsed: sidebarCollapsed }">
    <AppSidebar />
    <AppTopbar />

    <main class="workspace">
      <div class="workspace-inner">
        <RouterView />
      </div>
    </main>

    <ToastStack />
    <ConfirmDialog />
    <ApiKeyDialog />
  </div>
</template>
