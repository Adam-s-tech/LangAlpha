/**
 * An HITL resume answers in the agent mode of the run that raised the
 * interrupt: the server picks the graph that resumes the checkpoint by
 * agent_mode, and the composer's mode can flip while a question waits.
 * Everything else the resume reads (callbacks, runtime) is the current render's.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import { settleMountEffect } from './chatHookHarness';

vi.mock('react-i18next', () => {
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

import { sendChatMessageStream, sendHitlResponse } from '../../utils/api';
import { useChatMessages } from '../useChatMessages';

const mockSendStream = sendChatMessageStream as Mock;
const mockSendHitl = sendHitlResponse as Mock;

const SEND_AGENT_MODE_ARG = 7;
const HITL_AGENT_MODE_ARG = 6;

const questionInterrupt = {
  event: 'interrupt',
  interrupt_id: 'int-ask',
  action_requests: [{ type: 'ask_user_question', question: 'Which tickers?', options: [], allow_multiple: false }],
};

const planInterrupt = {
  event: 'interrupt',
  interrupt_id: 'int-plan',
  action_requests: [{ name: 'SubmitPlan', description: 'Step 1. Pull the filings.' }],
};

const fileArtifact = {
  event: 'artifact',
  agent: 'main',
  artifact_type: 'file_operation',
  artifact_id: 'file-1',
  payload: { operation: 'write_file', file_path: 'report.md' },
};

function sendRaises(interrupt: Record<string, unknown>) {
  mockSendStream.mockImplementation(async (...args: unknown[]) => {
    (args[5] as (e: Record<string, unknown>) => void)(interrupt);
    return { disconnected: false };
  });
}

/** The hook as ChatView mounts it, with the two inputs a test flips. */
function renderChat(initial: { agentMode: string; onFileArtifact: (e: unknown) => void }) {
  let props = initial;
  const rendered = renderHookWithProviders(() =>
    useChatMessages('ws-test', 'thread-1', null, null, null, null, props.onFileArtifact, null, props.agentMode),
  );
  const flip = (next: Partial<typeof initial>) => {
    props = { ...props, ...next };
    rendered.rerender();
  };
  return { ...rendered, flip };
}

describe('useChatMessages: HITL resume mode and freshness', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSendHitl.mockResolvedValue({ disconnected: false });
  });

  it('answers in the mode the interrupted run was sent in, not the mode shown now', async () => {
    sendRaises(questionInterrupt);
    const { result, flip } = renderChat({ agentMode: 'ptc', onFileArtifact: vi.fn() });
    await settleMountEffect();

    flip({ agentMode: 'flash' });
    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', false);
    });
    expect(mockSendStream.mock.calls[0][SEND_AGENT_MODE_ARG]).toBe('flash');
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    // The composer flips back while the question waits.
    flip({ agentMode: 'ptc' });
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA and AMD', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(mockSendHitl.mock.calls[0][HITL_AGENT_MODE_ARG]).toBe('flash');
  });

  it('routes the resumed stream to the callbacks of the current render', async () => {
    sendRaises(questionInterrupt);
    const armedWith = vi.fn();
    const current = vi.fn();
    const { result, flip } = renderChat({ agentMode: 'ptc', onFileArtifact: armedWith });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Screen chip names', false);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    flip({ onFileArtifact: current });
    mockSendHitl.mockImplementation(async (...args: unknown[]) => {
      (args[3] as (e: Record<string, unknown>) => void)(fileArtifact);
      return { disconnected: false };
    });
    await act(async () => {
      result.current.handleAnswerQuestion('NVDA and AMD', 'int-ask', 'int-ask');
    });

    await waitFor(() => expect(mockSendHitl).toHaveBeenCalledTimes(1));
    expect(current).toHaveBeenCalledWith(expect.objectContaining({ artifact_id: 'file-1' }));
    expect(armedWith).not.toHaveBeenCalled();
  });

  it('sends rejection feedback in the rejected run\'s mode, not as a fresh send', async () => {
    sendRaises(planInterrupt);
    const { result, flip } = renderChat({ agentMode: 'flash', onFileArtifact: vi.fn() });
    await settleMountEffect();

    await act(async () => {
      await result.current.handleSendMessage('Plan a filings review', true);
    });
    await waitFor(() => expect(result.current.pendingInterrupt).not.toBeNull());

    await act(async () => {
      result.current.handleRejectInterrupt();
    });
    flip({ agentMode: 'ptc' });
    await act(async () => {
      await result.current.handleSendMessage('Start with the 10-K instead', false);
    });

    expect(mockSendStream).toHaveBeenCalledTimes(1);
    expect(mockSendHitl).toHaveBeenCalledTimes(1);
    expect(mockSendHitl.mock.calls[0][2]).toEqual({
      'int-plan': { decisions: [{ type: 'reject', message: 'Start with the 10-K instead' }] },
    });
    expect(mockSendHitl.mock.calls[0][HITL_AGENT_MODE_ARG]).toBe('flash');
  });
});
