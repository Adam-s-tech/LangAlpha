import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from 'react';
import { AT_BOTTOM_PX, NEAR_BOTTOM_PX, isNearBottom } from '../../utils/scrollHelpers';

/**
 * Following a streaming transcript. A reader near the bottom is moved to the
 * new bottom in the frame the transcript grows, instantly: a timer of smooth
 * scrolls lagged a fast stream, absorbed the wheel input of a reader trying to
 * leave, and when a block landed mid-animation it stopped short, read as
 * "left", and never followed again. Direction decides intent: an upward scroll
 * leaves unless it ends at the very bottom, so a notch at a time gets away, and
 * a downward one rejoins inside the band. A fold that clamps scrollTop moves up
 * too, but lands on the bottom, so it keeps following.
 */
export interface StreamFollow {
  /** Reads a scroll event. False for a follow's own, which decides nothing.
   *  `judge` false leaves the flag alone, for a landing something else chose. */
  scrolled(judge?: boolean): boolean;
  /** Moves a following reader to the new bottom, for growth. */
  follow(): void;
  /** Follows again from the bottom wherever the reader was, for a send. */
  rejoin(): void;
}

export function createStreamFollow(c: HTMLElement, following: { current: boolean }): StreamFollow {
  let lastTop = c.scrollTop;
  // The scroll event of a follow arrives a frame after it. A block that lands
  // in between, a chart or a table, leaves the bottom further away than the
  // band, so that event is the follow's own and changes nothing. A reader who
  // scrolls in between is measured from where the follow put them, or a notch
  // up from there would read as a move down and be pulled back.
  let followTop: number | null = null;
  const stream: StreamFollow = {
    scrolled(judge = true) {
      const from = followTop ?? lastTop;
      const own = followTop !== null && Math.abs(c.scrollTop - followTop) < 1;
      followTop = null;
      lastTop = c.scrollTop;
      if (own) return false;
      if (!judge) return true;
      const metrics = { scrollTop: c.scrollTop, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight };
      following.current = isNearBottom(metrics, c.scrollTop < from ? AT_BOTTOM_PX : NEAR_BOTTOM_PX);
      return true;
    },
    follow() {
      if (!following.current) return;
      const bottom = c.scrollHeight - c.clientHeight;
      if (bottom <= c.scrollTop) return;
      followTop = bottom;
      c.scrollTo({ top: bottom });
    },
    rejoin() {
      following.current = true;
      stream.follow();
    },
  };
  return stream;
}

/**
 * The follow for a scroller that keeps no position to restore, so the first
 * observation follows too and a thread opens at its end. `shown` re-attaches
 * it when the content mounts. Later growth is followed only while `active`:
 * in a settled transcript it is the reader's own doing, a row opened, and
 * stays where they put it. The returned `rejoin` is for a send: the reader's
 * message and the reply land at the end, so a reader scrolled up would not
 * see either.
 */
export function useStreamFollow(
  scroller: RefObject<HTMLElement | null>,
  content: RefObject<HTMLElement | null>,
  shown: boolean,
  active: boolean,
): () => void {
  const activeRef = useRef(active);
  const streamRef = useRef<StreamFollow | null>(null);
  // The commit that ends a turn or a history load carries its last growth,
  // and the observer reports it only after this has marked the transcript
  // settled. Followed here instead, where layout already measures it.
  useLayoutEffect(() => {
    if (activeRef.current && !active) streamRef.current?.follow();
    activeRef.current = active;
  }, [active]);
  useEffect(() => {
    const c = scroller.current;
    const el = content.current;
    if (!c || !el) return;
    const stream = createStreamFollow(c, { current: true });
    streamRef.current = stream;
    let lastHeight = -1;
    const ro = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height ?? lastHeight;
      const follow = lastHeight < 0 || (height > lastHeight && activeRef.current);
      lastHeight = height;
      if (follow) stream.follow();
    });
    const onScroll = () => { stream.scrolled(); };
    c.addEventListener('scroll', onScroll, { passive: true });
    ro.observe(el);
    return () => {
      streamRef.current = null;
      c.removeEventListener('scroll', onScroll);
      ro.disconnect();
    };
  }, [scroller, content, shown]);
  return useCallback(() => streamRef.current?.rejoin(), []);
}
