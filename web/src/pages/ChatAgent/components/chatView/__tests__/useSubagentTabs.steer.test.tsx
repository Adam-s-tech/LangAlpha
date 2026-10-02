/**
 * Steering a subagent while it streams. The instruction shows at once as a
 * pending bubble, and the server delivers it only before the subagent's next
 * model call, so the message the subagent is writing keeps streaming with the
 * bubble on screen. Every stream write carries the task's whole transcript,
 * and the card keeps a longer list over a shorter one, so a bubble the
 * transcript does not hold froze the message until the delivery landed.
 *
 * A send that fails never reaches the queue, so no stream event will settle
 * its bubble; the send's own failure takes it back.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { mockFrames, runFrame, settleFrames } from '@/test/frames';
import { useCardState } from '../../../hooks/useCardState';
import { createStreamEventProcessor } from '../../../session/stream/processStreamEvent';
import { sendTaskInstruction } from '../../../session/subagents/sendTaskInstruction';
import type { TaskRefs } from '../../../session/streamRefs';
import type { SSEEvent } from '../../../session/types';
import { sendSubagentMessage } from '../../../utils/api';
import { useSubagentTabs } from '../useSubagentTabs';

vi.mock('../../../utils/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/api')>()),
  sendSubagentMessage: vi.fn(),
}));

type Processor = Parameters<typeof createStreamEventProcessor>;

const TASK = 'task:k7Xm2p';

beforeEach(() => {
  mockFrames();
  vi.mocked(sendSubagentMessage).mockReset().mockResolvedValue({});
});

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
  const subagentStateRefsRef = { current: refs.subagentStateRefs };
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
        sendSubagentInstruction: (tid: string, agentId: string, content: string) =>
          sendTaskInstruction(
            { t: (key: string) => key, updateSubagentCard: cardState.updateSubagentCard, subagentStateRefsRef },
            tid,
            agentId,
            content,
          ),
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
  // Starts a send and leaves it in flight; the returned promise settles with it.
  const startSteer = (content: string) => {
    let sent!: Promise<void>;
    act(() => {
      sent = rendered.result.current.handleSubagentInstruction(content);
    });
    return sent;
  };
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
  return { feed, text, steer, startSteer, frame, messages, ids };
}

describe('useSubagentTabs — steering a streaming subagent', () => {
  it('keeps the running message streaming under a pending instruction', async () => {
    const { text, steer, frame, messages } = harness();
    text('Revenue');
    frame();

    await steer('Focus on margins');
    text(' grew 12%');
    frame();

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue grew 12%' },
      { role: 'user', content: 'Focus on margins', isPending: true },
    ]);
  });

  it('confirms the pending bubble in place when the instruction is delivered', async () => {
    const { feed, text, steer, frame, messages, ids } = harness();
    text('Revenue');
    frame();
    await steer('Focus on margins');
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

  it('confirms every instruction one delivery drained, without a second bubble', async () => {
    const { feed, text, steer, frame, messages } = harness();
    text('Revenue');
    frame();
    await steer('Focus on margins');
    await steer('Skip 2019');

    // The middleware drains the whole queue before the next model call and
    // delivers it as one newline-joined instruction.
    feed({ event: 'steering_delivered', content: 'Focus on margins\nSkip 2019' });

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'user', content: 'Focus on margins' },
      { role: 'user', content: 'Skip 2019' },
    ]);
  });

  it('takes the bubble back when the run ends before delivering it', async () => {
    const { feed, text, steer, frame, messages } = harness();
    text('Revenue');
    frame();
    await steer('Focus on margins');

    feed({ event: 'steering_returned', content: 'Focus on margins' });

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'assistant', notice: 'chat.taskSteeringReturnedNotification' },
    ]);
  });

  it('takes the bubble back when the send is rejected', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    // The task finished between the user opening the input and sending.
    vi.mocked(sendSubagentMessage).mockRejectedValueOnce(
      Object.assign(new Error('Request failed with status code 409'), { response: { status: 409 } }),
    );
    const { text, steer, frame, messages } = harness();
    text('Revenue');
    frame();

    await steer('Focus on margins');

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'assistant', notice: 'chat.taskSteeringReturnedNotification' },
    ]);
    expect(sendSubagentMessage).toHaveBeenCalledWith('thread-1', 'k7Xm2p', 'Focus on margins');
  });

  it('says only that it was not sent when the send fails for another reason', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(sendSubagentMessage).mockRejectedValueOnce(new Error('Network Error'));
    const { text, steer, frame, messages } = harness();
    text('Revenue');
    frame();

    await steer('Focus on margins');

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'assistant', notice: 'chat.taskSteeringNotSentNotification' },
    ]);
  });

  it('adds no second notice when the run returned the instruction before its send failed', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let reject!: (err: unknown) => void;
    vi.mocked(sendSubagentMessage).mockReturnValueOnce(new Promise((_, r) => { reject = r; }));
    const { feed, text, startSteer, frame, messages } = harness();
    text('Revenue');
    frame();
    const sent = startSteer('Focus on margins');

    // The run's terminal sweep took the queued entry back, then the send's
    // own liveness recheck answered 409.
    feed({ event: 'steering_returned', content: 'Focus on margins' });
    await act(async () => {
      reject(new Error('Request failed with status code 409'));
      await sent;
    });

    expect(messages()).toEqual([
      { role: 'assistant', content: 'Revenue' },
      { role: 'assistant', notice: 'chat.taskSteeringReturnedNotification' },
    ]);
  });
});
