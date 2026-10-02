import { useEffect, useLayoutEffect, useState, type RefObject } from 'react';

/**
 * Observed element BORDER-BOX width, quantized to `step` px so per-pixel
 * resize drags (e.g. the MarketView panel divider) don't re-render subscribers
 * per frame. Returns 0 until measured, or when ResizeObserver is unavailable
 * (jsdom).
 *
 * `round: 'floor'` never reports width the element doesn't have — use it for
 * fit/layout budgets, where rounding up by half a step is what makes controls
 * overlap. Both the initial read and the observer read the border box, so a
 * caller subtracting its own border/padding gets a consistent number.
 */
export function useContainerWidth(
  ref: RefObject<HTMLElement | null>,
  step = 16,
  { round = 'round' }: { round?: 'round' | 'floor' } = {},
): number {
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;

    const quantize = round === 'floor' ? Math.floor : Math.round;
    const update = (w: number) => {
      const q = quantize(w / step) * step;
      setWidth((prev) => (prev === q ? prev : q));
    };

    update(el.getBoundingClientRect().width);

    const ro = new ResizeObserver(() => {
      // Read the border box off the element: `entry.contentRect` is the
      // CONTENT box, which would silently disagree with the initial read.
      update(el.getBoundingClientRect().width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, step, round]);

  return width;
}

/**
 * Whether the observed element's width is below `threshold`, or null until it
 * has been measured. Element-based (not viewport) so an embedded panel can
 * change layout independent of window width. The first read is a layout
 * effect, so the render that knows is the first one painted: a caller that
 * holds width-dependent content back while this is null never shows a frame
 * laid out for the wrong width. Without ResizeObserver (jsdom) it reports false.
 */
export function useNarrowContainer(
  ref: RefObject<HTMLElement | null>,
  threshold: number,
): boolean | null {
  const [narrow, setNarrow] = useState<boolean | null>(() => (typeof ResizeObserver === 'undefined' ? false : null));

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;

    const update = (width: number) => {
      setNarrow((prev) => {
        const next = width > 0 && width < threshold;
        return next === prev ? prev : next;
      });
    };

    update(el.getBoundingClientRect().width);

    const ro = new ResizeObserver(() => {
      // Border box, same as the initial read — `entry.contentRect` is the
      // CONTENT box, which disagrees on padded/scrollbar-bearing elements
      // and would flip `narrow` on the first no-op resize.
      update(el.getBoundingClientRect().width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, threshold]);

  return narrow;
}
