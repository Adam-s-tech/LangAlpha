import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import type { TurnEndScroll } from '@/lib/turnEndScroll';
import type { ChatMessage } from '@/types/chat';
import { useTranscriptFollow } from '../useTranscriptFollow';

vi.mock('../../../utils/scrollDom', () => ({
  findMessageElement: () => document.createElement('div'),
}));

const user = (id: string): ChatMessage => ({ id, role: 'user', content: 'q' } as ChatMessage);
const assistant = (id: string): ChatMessage => ({ id, role: 'assistant', content: 'a' } as ChatMessage);

interface Props {
  messages: ChatMessage[];
  isLoading: boolean;
  pendingInterrupt?: unknown;
  turnEndScroll?: TurnEndScroll;
}

function setup(initial: Props) {
  const follow = { rejoin: vi.fn(), landOnReply: vi.fn() };
  const starters = {
    handleSendMessage: vi.fn(async () => {}),
    handleEditMessage: vi.fn(async () => {}),
    handleRegenerate: vi.fn(async () => {}),
    handleRetry: vi.fn(async () => {}),
  };
  const scroller = document.createElement('div');
  const hook = renderHook(
    ({ turnEndScroll = 'reply_start', pendingInterrupt = null, ...state }: Props) =>
      useTranscriptFollow(
        { ...starters, ...state, pendingInterrupt, pendingRejection: null } as unknown as Parameters<typeof useTranscriptFollow>[0],
        follow,
        () => scroller,
        turnEndScroll,
      ),
    { initialProps: initial },
  );
  return { follow, starters, ...hook };
}

describe('useTranscriptFollow turn starters', () => {
  it.each([
    ['handleSendMessage', ['hi', false, null, null, {}]],
    ['handleEditMessage', ['u1', 'edited', {}]],
    ['handleRegenerate', ['a1', {}]],
    ['handleRetry', [{}]],
  ] as const)('%s starts the turn, then rejoins the follow', (name, args) => {
    const { follow, starters, result } = setup({ messages: [], isLoading: false });
    const started = (result.current[name] as (...a: unknown[]) => unknown)(...args);

    expect(starters[name]).toHaveBeenCalledWith(...args);
    expect(starters[name].mock.invocationCallOrder[0]).toBeLessThan(follow.rejoin.mock.invocationCallOrder[0]);
    expect(started).toBe(starters[name].mock.results[0].value);
  });
});

describe('useTranscriptFollow turn end', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lands instantly when the reader prefers reduced motion', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce'), media: q, addEventListener() {}, removeEventListener() {} }));
    const before = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: before, isLoading: false });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isLoading: true });
    rerender({ messages: pending, isLoading: false });
    expect(follow.landOnReply).toHaveBeenCalledWith('a2', 'auto');
  });

  it('lands on the reply that the turn produced', () => {
    const before = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: before, isLoading: false });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isLoading: true });
    rerender({ messages: pending, isLoading: false });
    expect(follow.landOnReply).toHaveBeenCalledWith('a2', 'smooth');
  });

  it('leaves a finished turn at the end under the default preference', () => {
    const before = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: before, isLoading: false, turnEndScroll: 'bottom' });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isLoading: true, turnEndScroll: 'bottom' });
    rerender({ messages: pending, isLoading: false, turnEndScroll: 'bottom' });
    expect(follow.landOnReply).not.toHaveBeenCalled();
  });

  it('waits out an interrupt, which pauses the turn rather than ending it', () => {
    const before = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: before, isLoading: false });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isLoading: true });
    rerender({ messages: pending, isLoading: false, pendingInterrupt: { id: 'i1' } });
    expect(follow.landOnReply).not.toHaveBeenCalled();
    rerender({ messages: pending, isLoading: true });
    rerender({ messages: pending, isLoading: false });
    expect(follow.landOnReply).toHaveBeenCalledWith('a2', 'smooth');
  });

  it('stays put when a preflight rolls the old transcript back', () => {
    // A regenerate flips loading on, replaces the reply with a placeholder,
    // then restores the snapshot when the checkpoint fetch fails: no turn ended.
    const before = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: before, isLoading: false });
    rerender({ messages: [user('u1'), assistant('assistant-pending-1')], isLoading: true });
    rerender({ messages: before, isLoading: false });
    expect(follow.landOnReply).not.toHaveBeenCalled();
  });

  it('lands after a reload that opened mid-turn', () => {
    const streaming = [user('u1'), assistant('a1')];
    const { follow, rerender } = setup({ messages: streaming, isLoading: true });
    rerender({ messages: [user('u1'), { ...assistant('a1') }], isLoading: false });
    expect(follow.landOnReply).toHaveBeenCalledWith('a1', 'smooth');
  });
});
