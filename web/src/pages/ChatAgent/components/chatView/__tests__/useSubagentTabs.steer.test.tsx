/**
 * Steering a subagent while it streams. The instruction shows at once as a
 * pending bubble, and the server delivers it only before the subagent's next
 * model call, so the message the subagent is writing keeps streaming with the
 * bubble on screen. Every stream write carries the task's whole transcript,
 * and the card keeps a longer list over a shorter one, so a bubble the
 * transcript does not hold froze the message until the delivery landed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { mockFrames, runFrame, settleFrames } from '@/test/frames';
import { useCardState } from '../../../hooks/useCardState';
import { createStreamEventProcessor } from '../../../session/stream/processStreamEvent';
import { addPendingTaskInstruction } from '../../../session/subagents/liveEventHandlers';
import type { TaskRefs } from '../../../session/streamRefs';
import type { SSEEvent } from '../../../session/types';
import { useSubagentTabs } from '../useSubagentTabs';

type Processor = Parameters<typeof createStreamEventProcessor>;

const TASK = 'task:k7Xm2p';

beforeEach(mockFrames);

afterEach(() => {
  settleFrames();
  vi.restoreAllMocks();
});

function harness() {
  const refs = {
    contentOrderCounterRef: { current: 0 },
    currentReasoningIdRef: { current: null },
    currentToolCallIdRef: { current: null },
    subagentStateRefs: {} as Record<string, TaskRefs>,
  };
  const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;
  const rendered = renderHook(
    () => {
      const cardState = useCardState();
      const [activeAgentId, setActiveAgentId] = React.useState(TASK);
      const activeAgentIdRef = React.useRef(TASK);
      const tabs = useSubagentTabs({
        threadId: 'thread-1',
        workspaceId: 'ws-1',
        // The task's own URL, with the deep-link refresh held off: the card
        // here is the one the stream builds.
        initialTaskId: TASK.slice('task:'.length),
        isLoadingHistory: true,
        activeAgentId,
        setActiveAgentId,
        cards: cardState.cards,
        updateSubagentCard: cardState.updateSubagentCard,
        // How useChatMessages binds it: to the transcripts the stream writes.
        addSubagentInstruction: (agentId: string, content: string) =>
          addPendingTaskInstruction({
            taskId: agentId,
            content,
            subagentStateRefs: refs.subagentStateRefs,
            updateSubagentCard: cardState.updateSubagentCard,
          }),
        getSubagentHistory: (() => null) as never,
        resolveSubagentIdToAgentId: ((id: string) => id) as never,
        hydrateTaskTranscript: (async () => null) as never,
        saveScrollPosition: () => {},
        scrollPositionsRef: { current: {} },
        skipSubagentAutoScrollRef: { current: false },
        activeAgentIdRef,
        resolvedThreadIdRef: { current: 'thread-1' },
      });
      return { ...tabs, cards: cardState.cards, updateSubagentCard: cardState.updateSubagentCard };
    },
    { wrapper },
  );
  const rt = {
    setMessages: vi.fn(),
    queueMessages: vi.fn(),
    flushMessages: vi.fn(),
    updateSubagentCard: (...args: Parameters<typeof rendered.result.current.updateSubagentCard>) =>
      rendered.result.current.updateSubagentCard(...args),
    lastEventIdRef: { current: null },
    threadIdRef: { current: null },
    t: (key: string) => key,
  };
  const process = createStreamEventProcessor(
    rt as unknown as Processor[0],
    { clearModelStatus: () => {} } as unknown as Processor[1],
    'a1',
    refs as unknown as Processor[3],
    (event) => (event.agent as string) ?? null,
  );
  const feed = (event: Record<string, unknown>) => act(() => process({ agent: TASK, ...event } as SSEEvent));
  const text = (content: string) => feed({ event: 'message_chunk', content_type: 'text', content });
  const steer = (content: string) => act(() => rendered.result.current.handleSubagentInstruction(content));
  const frame = () => act(() => runFrame());
  const card = () => rendered.result.current.cards[`subagent-${TASK}`]?.subagentData?.messages ?? [];
  const messages = () =>
    card().map((m) => {
      const segments = (m.contentSegments ?? []) as { type: string; content?: string }[];
      const notice = segments.find((seg) => seg.type === 'notification')?.content;
      return {
        role: m.role,
        ...(notice ? { notice } : { content: m.content }),
        ...(m.isPending ? { isPending: true } : {}),
      };
    });
  const ids = () => card().map((m) => m.id);
  return { feed, text, steer, frame, messages, ids };
}

describe('useSubagentTabs — steering a streaming subagent', () => {
  it('keeps the running message streaming under a pending instruction', () => {
    const { text, steer, frame, messages } = harness();
    text('Revenue');
    frame();

    steer('Focus on margins');
    text(' grew 12%');
    frame();

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue grew 12%' },
      { role: 'user', content: 'Focus on margins', isPending: true },
    ]);
  });

  it('confirms the pending bubble in place when the instruction is delivered', () => {
    const { feed, text, steer, frame, messages, ids } = harness();
    text('Revenue');
    frame();
    steer('Focus on margins');
    const bubbleId = ids()[1];

    feed({ event: 'steering_delivered', content: 'Focus on margins' });
    text('Margins widened');
    frame();

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'user', content: 'Focus on margins' },
      { role: 'assistant', content: 'Margins widened' },
    ]);
    expect(ids()[1]).toBe(bubbleId);
  });

  it('confirms every instruction one delivery drained, without a second bubble', () => {
    const { feed, text, steer, frame, messages } = harness();
    text('Revenue');
    frame();
    steer('Focus on margins');
    steer('Skip 2019');

    // The middleware drains the whole queue before the next model call and
    // delivers it as one newline-joined instruction.
    feed({ event: 'steering_delivered', content: 'Focus on margins\nSkip 2019' });

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'user', content: 'Focus on margins' },
      { role: 'user', content: 'Skip 2019' },
    ]);
  });

  it('takes the bubble back when the run ends before delivering it', () => {
    const { feed, text, steer, frame, messages } = harness();
    text('Revenue');
    frame();
    steer('Focus on margins');

    feed({ event: 'steering_returned', content: 'Focus on margins' });

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'assistant', notice: 'chat.taskSteeringReturnedNotification' },
    ]);
  });
});
