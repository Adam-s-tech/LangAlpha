// Pure scroll decision logic for the chat transcript. Kept free of DOM/layout
// so it is unit-testable under jsdom (which has no layout).

/** Distance from the bottom (px) a downward scroll still counts as at the
 *  bottom, rejoining a streaming follow. */
export const NEAR_BOTTOM_PX = 120;
/** "At the bottom" for an upward scroll. Not 0: scrollHeight and clientHeight
 *  are rounded and scrollTop is not, and under browser zoom a container at
 *  its maximum reads a residual of a pixel or two. */
export const AT_BOTTOM_PX = 4;

export interface ScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** Distance from the bottom is within `threshold` px (inclusive). */
export function isNearBottom(m: ScrollMetrics, threshold = 120): boolean {
  return m.scrollHeight - m.scrollTop - m.clientHeight <= threshold;
}
