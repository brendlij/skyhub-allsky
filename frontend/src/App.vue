<script setup>
import { RouterView } from "vue-router";
import AppSidebar from "./components/layout/AppSidebar.vue";
import AppTopbar from "./components/layout/AppTopbar.vue";
import ConfirmDialog from "./components/ui/ConfirmDialog.vue";
import ToastStack from "./components/ui/ToastStack.vue";
import { useTheme } from "./composables/useTheme";

const { sidebarCollapsed } = useTheme();
</script>

<template>
  <div class="shell" :class="{ collapsed: sidebarCollapsed }">
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
