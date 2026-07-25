import { ref } from "vue";

/* Destructive actions previously fired on a single click - deleting a node, and
 * replacing every overlay entity with a preset. This gives them a confirm step
 * without scattering window.confirm() through the views. */

const request = ref(null);
let resolvePending = null;

function settle(result) {
  request.value = null;

  if (resolvePending) {
    const resolve = resolvePending;
    resolvePending = null;
    resolve(result);
  }
}

export function confirmAction({
  title,
  message = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger"
} = {}) {
  // A second request while one is open resolves the first as cancelled rather
  // than leaving its promise dangling forever.
  if (resolvePending) settle(false);

  request.value = { title, message, confirmLabel, cancelLabel, tone };

  return new Promise((resolve) => {
    resolvePending = resolve;
  });
}

export function useConfirm() {
  return {
    request,
    confirmAction,
    accept: () => settle(true),
    cancel: () => settle(false)
  };
}
