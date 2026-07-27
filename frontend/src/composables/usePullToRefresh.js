import { onBeforeUnmount, ref, watch } from "vue";

/* Pull down at the top of a scroller to reload.
 *
 * The gesture only moves an indicator, never the page content: dragging the
 * whole layout costs a repaint of the live image on every frame, and a spinner
 * that tracks the finger reads as responsive on its own.
 */

const THRESHOLD = 72;
const MAX_PULL = 120;
// Resistance, so the indicator lags the finger the way a native list does.
const DAMPING = 0.5;

export function usePullToRefresh(targetRef, onRefresh, { enabled } = {}) {
  const distance = ref(0);
  const refreshing = ref(false);

  let startY = null;

  function isEnabled() {
    return enabled ? Boolean(enabled.value) : true;
  }

  function onTouchStart(event) {
    const element = targetRef.value;

    if (!isEnabled() || refreshing.value || !element || element.scrollTop > 0) return;

    startY = event.touches[0].clientY;
  }

  function onTouchMove(event) {
    if (startY === null) return;

    const element = targetRef.value;
    const delta = event.touches[0].clientY - startY;

    // Scrolling away from the top, or upwards, ends the gesture rather than
    // fighting the scroller for it.
    if (delta <= 0 || (element && element.scrollTop > 0)) {
      distance.value = 0;
      startY = null;
      return;
    }

    distance.value = Math.min(MAX_PULL, delta * DAMPING);
  }

  async function onTouchEnd() {
    if (startY === null) return;

    startY = null;

    if (distance.value < THRESHOLD) {
      distance.value = 0;
      return;
    }

    refreshing.value = true;
    distance.value = THRESHOLD;

    try {
      await onRefresh();
    } finally {
      refreshing.value = false;
      distance.value = 0;
    }
  }

  function bind(element) {
    if (!element) return;

    element.addEventListener("touchstart", onTouchStart, { passive: true });
    element.addEventListener("touchmove", onTouchMove, { passive: true });
    element.addEventListener("touchend", onTouchEnd, { passive: true });
    element.addEventListener("touchcancel", onTouchEnd, { passive: true });
  }

  function unbind(element) {
    if (!element) return;

    element.removeEventListener("touchstart", onTouchStart);
    element.removeEventListener("touchmove", onTouchMove);
    element.removeEventListener("touchend", onTouchEnd);
    element.removeEventListener("touchcancel", onTouchEnd);
  }

  watch(targetRef, (element, previous) => {
    unbind(previous);
    bind(element);
  }, { immediate: true });

  onBeforeUnmount(() => unbind(targetRef.value));

  return { distance, refreshing, threshold: THRESHOLD };
}
