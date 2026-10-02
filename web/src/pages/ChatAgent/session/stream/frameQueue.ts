type Update<T> = (prev: T) => T;

/**
 * Per-token state updates, applied once per animation frame.
 *
 * A model streams one SSE event per chunk, and each one rendered the whole
 * transcript, although the screen can only show one state per frame. Queued
 * here, every chunk that lands between two frames is composed into a single
 * update. The batch follows the display: a 120 Hz screen drains it twice as
 * often as a 60 Hz one, and a main thread too busy to produce frames drains
 * it less often, which is when batching saves the most.
 *
 * Everything else must apply the queue before its own write, in the same task
 * (`take`), so no other state ever gets ahead of the text.
 */
export interface FrameQueue<T> {
  /** Apply on the next frame, after every update queued before it. */
  queue: (update: Update<T>) => void;
  /** Remove and return what is queued, composed into one update. */
  take: () => Update<T> | null;
  /** Apply what is queued now. */
  flush: () => void;
  /** Drop what is queued. */
  cancel: () => void;
}

// One frame callback for every queue, so two streaming views cost one render.
const waiting = new Set<() => void>();
let frame = 0;
let listening = false;

function flushWaiting(): void {
  const flushes = [...waiting];
  waiting.clear();
  for (const flush of flushes) flush();
}

// A plain default-priority update, not flushSync: it can render in the same
// pass as the typewriter's own per-frame update. Forced to render inside the
// frame callback, the two always rendered apart, and a stream slower than the
// display took 44% more commits and 38% more script time than no queue at all.
function onFrame(): void {
  frame = 0;
  flushWaiting();
}

// A hidden document runs no frame callbacks: hand over what is waiting now
// rather than when the page comes back.
function onVisibilityChange(): void {
  if (document.visibilityState !== 'visible') flushWaiting();
}

export function createFrameQueue<T>(apply: (update: Update<T>) => void): FrameQueue<T> {
  let pending: Update<T>[] = [];

  function take(): Update<T> | null {
    waiting.delete(flush);
    if (pending.length === 0) return null;
    const batch = pending;
    pending = [];
    return batch.length === 1 ? batch[0] : (prev) => batch.reduce((acc, update) => update(acc), prev);
  }

  function flush(): void {
    const update = take();
    if (update) apply(update);
  }

  function queue(update: Update<T>): void {
    pending.push(update);
    if (document.visibilityState !== 'visible') {
      flush();
      return;
    }
    waiting.add(flush);
    if (!listening) {
      listening = true;
      document.addEventListener('visibilitychange', onVisibilityChange);
    }
    if (!frame) frame = requestAnimationFrame(onFrame);
  }

  function cancel(): void {
    waiting.delete(flush);
    pending = [];
  }

  return { queue, take, flush, cancel };
}
