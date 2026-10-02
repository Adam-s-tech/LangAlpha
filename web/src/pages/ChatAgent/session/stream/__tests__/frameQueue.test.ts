import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createFrameQueue } from '../frameQueue';

type Update = (prev: string[]) => string[];

let frames: FrameRequestCallback[] = [];
let visibility: DocumentVisibilityState = 'visible';

const runFrame = () => {
  const due = frames;
  frames = [];
  for (const cb of due) cb(performance.now());
};

function setup() {
  let state: string[] = [];
  const applied: Update[] = [];
  const apply = (update: Update) => {
    applied.push(update);
    state = update(state);
  };
  const q = createFrameQueue<string[]>(apply);
  const push = (s: string): Update => (prev) => [...prev, s];
  return { q, push, applied, read: () => state };
}

beforeEach(() => {
  frames = [];
  visibility = 'visible';
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => frames.push(cb));
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
});

afterEach(() => {
  runFrame();
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

  it('applies at once in a hidden document, where no frame would come', () => {
    const { q, push, read } = setup();
    visibility = 'hidden';
    q.queue(push('a'));
    expect(read()).toEqual(['a']);
    expect(frames).toHaveLength(0);
  });

  it('applies what is waiting when the page hides', () => {
    const { q, push, read } = setup();
    q.queue(push('a'));
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    expect(read()).toEqual(['a']);
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

  it('cancel drops what is queued', () => {
    const { q, push, applied } = setup();
    q.queue(push('a'));
    q.cancel();
    runFrame();
    expect(applied).toHaveLength(0);
  });
});
