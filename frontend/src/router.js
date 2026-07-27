import { createRouter, createWebHistory } from "vue-router";
import MonitorView from "./views/MonitorView.vue";
import CaptureExplorerView from "./views/CaptureExplorerView.vue";
import SettingsView from "./views/SettingsView.vue";
import NodesView from "./views/NodesView.vue";
import OverlaysView from "./views/OverlaysView.vue";
import LoginView from "./views/LoginView.vue";
import { authState, refreshAuthState } from "./api/auth";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/monitor" },
    { path: "/login", component: LoginView, meta: { public: true } },
    { path: "/monitor", component: MonitorView },
    { path: "/captures", component: CaptureExplorerView },
    { path: "/overlays", component: OverlaysView },
    { path: "/settings", component: SettingsView },
    { path: "/nodes", component: NodesView }
  ]
});

/* This guard is convenience, not security.
 *
 * Every view below renders nothing without data, and every request for that data
 * is authorised on the server. Defeating the guard in devtools gets you an empty
 * dashboard and a column of 401s, not access. Its job is to send someone whose
 * session just expired to a login form instead of a page that quietly fails.
 */
router.beforeEach(async (to) => {
  // One status call per page load. After that the cached state is authoritative
  // until a 401 somewhere marks it stale.
  if (authState.value.loading) await refreshAuthState();

  if (to.meta.public) {
    // Signed in already and heading for the login page: there is nothing there.
    if (authState.value.authenticated) return { path: "/monitor" };

    return true;
  }

  if (authState.value.authenticated) return true;

  return {
    path: "/login",
    query: to.fullPath === "/" ? {} : { redirect: to.fullPath }
  };
});
