import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createFrameQueue } from '../frameQueue';
import { isPageUnseen } from '@/lib/pageVisibility';

type Update = (prev: string[]) => string[];

let frames: FrameRequestCallback[] = [];
let visibility: DocumentVisibilityState = 'visible';
// Frame times as a display reports them: a 60 Hz one unless a test says
// otherwise, never going back across tests.
let clock = 0;

const runFrame = (interval = 1000 / 60) => {
  clock += interval;
  const due = frames;
  frames = [];
  for (const cb of due) cb(clock);
};

function setup() {
  let state: string[] = [];
  const applied: Update[] = [];
  const unseen: boolean[] = [];
  const apply = (update: Update) => {
    applied.push(update);
    unseen.push(isPageUnseen());
    state = update(state);
  };
  const q = createFrameQueue<string[]>(apply);
  const push = (s: string): Update => (prev) => [...prev, s];
  return { q, push, applied, unseen, read: () => state };
}

function setVisibility(next: DocumentVisibilityState) {
  visibility = next;
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  frames = [];
  visibility = 'visible';
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => frames.push(cb));
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => visibility !== 'visible');
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
});

afterEach(() => {
  if (visibility !== 'visible') setVisibility('visible');
  runFrame();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('createFrameQueue', () => {
  it('applies everything queued before a frame as one update, in order', () => {
    const { q, push, applied, read } = setup();
    q.queue(push('a'));
    q.queue(push('b'));
    q.queue(push('c'));
    expect(read()).toEqual([]);
    runFrame();
    expect(applied).toHaveLength(1);
    expect(read()).toEqual(['a', 'b', 'c']);
  });

  it('take hands the queue to the caller and leaves the frame nothing to apply', () => {
    const { q, push, applied } = setup();
    q.queue(push('a'));
    q.queue(push('b'));
    const taken = q.take();
    expect(taken?.(['x'])).toEqual(['x', 'a', 'b']);
    runFrame();
    expect(applied).toHaveLength(0);
    expect(q.take()).toBeNull();
  });

  it('batches a hidden page on a timer, where no frame would come', () => {
    const { q, push, applied, read } = setup();
    setVisibility('hidden');
    q.queue(push('a'));
    q.queue(push('b'));
    expect(read()).toEqual([]);
    expect(frames).toHaveLength(0);
    vi.advanceTimersByTime(500);
    expect(applied).toHaveLength(1);
    expect(read()).toEqual(['a', 'b']);
  });

  it('applies what is waiting when the page hides', () => {
    const { q, push, read } = setup();
    q.queue(push('a'));
    setVisibility('hidden');
    expect(read()).toEqual(['a']);
  });

  it('applies what the hidden page held on its return, at once and still unseen', () => {
    const { q, push, applied, unseen, read } = setup();
    setVisibility('hidden');
    q.queue(push('a'));
    setVisibility('visible');
    expect(read()).toEqual(['a']);
    expect(unseen).toEqual([true]);
    expect(isPageUnseen()).toBe(false);
    vi.advanceTimersByTime(500);
    runFrame();
    expect(applied).toHaveLength(1);
  });

  it('serves every queue from one frame callback', () => {
    const one = setup();
    const two = setup();
    one.q.queue(one.push('a'));
    two.q.queue(two.push('b'));
    expect(frames).toHaveLength(1);
    runFrame();
    expect(one.read()).toEqual(['a']);
    expect(two.read()).toEqual(['b']);
  });

  it('drains at most once per 60 Hz frame on a faster screen', () => {
    const { q, push, applied, read } = setup();
    q.queue(push('a'));
    runFrame();
    expect(read()).toEqual(['a']);

    q.queue(push('b'));
    runFrame(1000 / 120);
    expect(applied).toHaveLength(1);
    expect(frames).toHaveLength(1);
    runFrame(1000 / 120);
    expect(read()).toEqual(['a', 'b']);
  });

  it('cancel drops what is queued', () => {
    const { q, push, applied } = setup();
    q.queue(push('a'));
    q.cancel();
    runFrame();
    expect(applied).toHaveLength(0);
  });
});
