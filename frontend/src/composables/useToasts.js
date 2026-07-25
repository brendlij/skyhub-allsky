import { ref } from "vue";

/* Replaces the single global `message` string, which showed errors and successes
 * identically, could not be dismissed, and silently overwrote itself. */

const toasts = ref([]);
let nextId = 1;

const DEFAULT_TIMEOUT = { success: 3200, info: 3800, warning: 6500, error: 0 };

function dismissToast(id) {
  toasts.value = toasts.value.filter((toast) => toast.id !== id);
}

function pushToast(message, { tone = "info", title = null, timeout = null } = {}) {
  const text = String(message ?? "").trim();

  if (!text) return null;

  // Collapse a repeat of the newest toast instead of stacking duplicates, which
  // is what a reconnect loop or a retried request tends to produce.
  const newest = toasts.value[toasts.value.length - 1];

  if (newest && newest.message === text && newest.tone === tone) {
    newest.count += 1;
    return newest.id;
  }

  const id = nextId++;
  const life = timeout ?? DEFAULT_TIMEOUT[tone] ?? 4000;

  toasts.value = [...toasts.value, { id, message: text, tone, title, count: 1 }];

  // Errors stay until dismissed; anything transient clears itself.
  if (life > 0) {
    window.setTimeout(() => dismissToast(id), life);
  }

  return id;
}

export function useToasts() {
  return {
    toasts,
    pushToast,
    dismissToast,
    notify: (message, options) => pushToast(message, { tone: "success", ...options }),
    notifyError: (error, options) => pushToast(
      error?.message || String(error) || "Something went wrong",
      { tone: "error", ...options }
    ),
    clearToasts: () => { toasts.value = []; }
  };
}
