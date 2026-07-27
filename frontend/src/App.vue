<script setup>
import { computed, ref, watch } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";
import AppSidebar from "./components/layout/AppSidebar.vue";
import AppTopbar from "./components/layout/AppTopbar.vue";
import BottomNav from "./components/mobile/BottomNav.vue";
import MobileHeader from "./components/mobile/MobileHeader.vue";
import ConfirmDialog from "./components/ui/ConfirmDialog.vue";
import ToastStack from "./components/ui/ToastStack.vue";
import { authState } from "./api/auth";
import { stopRealtime, useSkyHub } from "./composables/useSkyHub";
import { usePullToRefresh } from "./composables/usePullToRefresh";
import { useTheme } from "./composables/useTheme";
import { useViewport } from "./composables/useViewport";

/* Two shells, one application.
 *
 * Below 1024px the sidebar and top bar are replaced by an app bar and a bottom
 * navigation; above it, the desktop layout is untouched. The views themselves
 * are shared - they adapt inside, rather than being forked per platform.
 *
 * The login page is a third case: no chrome at all. Wrapping a sign-in form in
 * navigation to pages it cannot open is just an invitation to click them.
 */

const { sidebarCollapsed } = useTheme();
const { isMobile } = useViewport();
const { refreshDashboard } = useSkyHub();

const route = useRoute();
const router = useRouter();

const chromeless = computed(() => route.meta.public === true);

/* A session can end while the tab sits open - it idled out, or it was revoked
 * from another browser. The first 401 flips this, and here is where the app
 * reacts to it: tear down the live socket and get out of a UI that can no
 * longer load anything. */
watch(() => authState.value.authenticated, (authenticated) => {
  if (authenticated || authState.value.loading) return;

  stopRealtime();

  if (!route.meta.public) {
    router.replace({ path: "/login", query: { redirect: route.fullPath } });
  }
});

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
  <div v-if="chromeless" class="shell-bare">
    <RouterView />

    <ToastStack />
    <ConfirmDialog />
  </div>

  <div v-else-if="isMobile" class="shell-mobile">
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
  </div>
</template>
