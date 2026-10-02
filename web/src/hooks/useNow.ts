import { useEffect, useState } from 'react';

type Listener = (now: number) => void;

interface Clock {
  listeners: Set<Listener>;
  timer?: ReturnType<typeof setTimeout>;
}

const clocks = new Map<number, Clock>();

function publish(clock: Clock): void {
  const now = Date.now();
  clock.listeners.forEach((listener) => listener(now));
}

// On the boundary rather than every `interval` from whenever the first reader
// mounted, so a minute clock turns with the wall clock's minute.
function schedule(interval: number, clock: Clock): void {
  clock.timer = setTimeout(() => {
    publish(clock);
    schedule(interval, clock);
  }, interval - (Date.now() % interval));
}

// A hidden tab's timers are throttled to a minute or more, so a tab coming back
// catches up at once rather than showing stale times until its next tick.
function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') clocks.forEach(publish);
}

function subscribe(interval: number, listener: Listener): () => void {
  let clock = clocks.get(interval);
  if (!clock) {
    clock = { listeners: new Set() };
    clocks.set(interval, clock);
    schedule(interval, clock);
    if (clocks.size === 1) document.addEventListener('visibilitychange', onVisibilityChange);
  }
  const joined = clock;
  joined.listeners.add(listener);
  return () => {
    joined.listeners.delete(listener);
    if (joined.listeners.size) return;
    clearTimeout(joined.timer);
    clocks.delete(interval);
    if (!clocks.size) document.removeEventListener('visibilitychange', onVisibilityChange);
  };
}

/**
 * The wall clock as a render input, advancing every `interval` ms while
 * `enabled`. Anything rendered from the time ("5m ago", an elapsed counter,
 * which day is today) reads it from here, never from `Date.now()` in render:
 * the React Compiler caches a computation on its inputs, so a clock read
 * inside one stops moving after the first render. One timer per interval is
 * shared by every reader. A label that stops changing at a deadline passes
 * `enabled` as a test of the time, so its clock stops at the deadline too.
 */
export function useNow(interval = 60_000, enabled: boolean | ((now: number) => boolean) = true): number {
  const [now, setNow] = useState(Date.now);
  const ticking = typeof enabled === 'function' ? enabled(now) : enabled;
  useEffect(() => {
    if (!ticking) return;
    // A reader re-enabled after a pause catches up now instead of on the next
    // boundary; on mount the initial state is already current, so it bails.
    setNow((prev) => {
      const current = Date.now();
      return current - prev >= interval ? current : prev;
    });
    return subscribe(interval, setNow);
  }, [interval, ticking]);
  return now;
}
