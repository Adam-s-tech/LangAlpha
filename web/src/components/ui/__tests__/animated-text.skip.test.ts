import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

// framer with its animations skipped (a hidden tab, see lib/framer) resolves an
// animation's `finished` at once, so animate()'s onComplete runs in a microtask,
// but applies the final value only on its next frame: on return, after the
// completion. This mock keeps exactly that order; `frames` is framer's queue.
type Opts = { onUpdate: (v: number) => void; onComplete: () => void };
let frames: (() => void)[] = [];
vi.mock('@/lib/framer', () => ({
  animate: (_from: number, to: number, opts: Opts) => {
    frames.push(() => opts.onUpdate(to));
    queueMicrotask(opts.onComplete);
    return { stop: () => {} };
  },
}));

import { useAnimatedText } from '../animated-text';

const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ') + ' ';

describe('useAnimatedText with framer skipping animations', () => {
  beforeEach(() => {
    frames = [];
  });

  it('keeps the last word of a reply that ended while hidden', async () => {
    const { result, rerender } = renderHook(({ text, enabled }) => useAnimatedText(text, { enabled }), {
      initialProps: { text: 'seed ', enabled: true },
    });
    const text = 'seed ' + words(10) + 'end.';
    await act(async () => rerender({ text, enabled: true }));
    await act(async () => rerender({ text, enabled: false }));
    expect(result.current).toBe(text);
    // The page comes back and framer runs the frame it queued.
    act(() => {
      for (const run of frames.splice(0)) run();
    });
    expect(result.current).toBe(text);
  });
});
