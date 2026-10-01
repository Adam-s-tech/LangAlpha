import { useEffect, useState } from 'react';

/**
 * The transcript with every streamed chunk applied.
 *
 * A chunk only grows a message that is already there, and nearly everything
 * that reads the transcript needs only which messages there are. So chunk
 * flushes land here alone, and reach the few readers that show the text
 * (subscribed through `useLiveMessages`) without rendering the view that holds
 * the transcript. Every other write lands here first and then in that view's
 * state, so the state has the structure at once and the text as of its last
 * write.
 */
export interface LiveMessages<T> {
  get: () => T;
  set: (value: T) => void;
  subscribe: (listener: () => void) => () => void;
}

export function createLiveMessages<T>(initial: T): LiveMessages<T> {
  let value = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set: (next) => {
      if (Object.is(next, value)) return;
      value = next;
      for (const listener of [...listeners]) listener();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * The store's value, rendered on every change.
 *
 * State and a subscription rather than useSyncExternalStore: a store update
 * renders on the sync lane, apart from the typewriter's default-lane update of
 * the same frame, which took 40% more commits. As state it batches with them,
 * and with whatever else the same task sets, so one commit never shows the
 * new transcript beside the old loading state.
 */
export function useLiveMessages<T>(store: LiveMessages<T>): T {
  const [value, setValue] = useState(store.get);
  useEffect(() => {
    const update = () => setValue(store.get());
    // A chunk can land between this render and the subscription.
    update();
    return store.subscribe(update);
  }, [store]);
  return value;
}
