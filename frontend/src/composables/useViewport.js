import { ref } from "vue";

/* One source of truth for "is this a phone".
 *
 * The breakpoints live here rather than in each component so a layout decision
 * in script and a media query in CSS cannot drift apart. 1024 is where the
 * desktop sidebar stops fitting next to a usable image; 600 is where two
 * columns of metrics stop being comfortable.
 */

const MOBILE_QUERY = "(max-width: 1023.98px)";
const COMPACT_QUERY = "(max-width: 599.98px)";
const COARSE_QUERY = "(hover: none) and (pointer: coarse)";

// Module level, so the whole app shares one listener per query instead of adding
// one per component instance. They live as long as the page does, which is why
// nothing here unsubscribes.
function watchQuery(query) {
  const matches = ref(false);

  if (typeof window === "undefined" || !window.matchMedia) return matches;

  const media = window.matchMedia(query);
  matches.value = media.matches;
  media.addEventListener("change", (event) => { matches.value = event.matches; });

  return matches;
}

const isMobile = watchQuery(MOBILE_QUERY);
const isCompact = watchQuery(COMPACT_QUERY);
const isTouch = watchQuery(COARSE_QUERY);

export function useViewport() {
  return { isMobile, isCompact, isTouch };
}
