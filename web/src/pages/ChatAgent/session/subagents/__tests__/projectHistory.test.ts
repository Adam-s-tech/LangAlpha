// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { projectSubagentHistory } from '../projectHistory';
import { createSubagentHistoryStore, type SubagentHistorySnapshot } from '../historyStore';
import type { SubagentRuntime } from '../../runtime';
import type { SSEEvent } from '../../types';

const makeRuntime = (onChange?: (s: SubagentHistorySnapshot) => void) => {
  const rt = {
    t: (key: string) => key,
    subagentHistory: createSubagentHistoryStore(onChange),
    subagentStateRefsRef: { current: {} },
  };
  return rt as unknown as SubagentRuntime;
};

const lifecycle = (fields: Record<string, unknown>) =>
  ({ event: 'workflow_lifecycle', ...fields }) as unknown as SSEEvent;

/** Replay ghost lane: table-sourced metadata events only, no transcript. */
const ghostEvents = [
  { event: 'provenance', tool_call_id: 'tc-1', sources: [] },
  { event: 'context_window', action: 'token_usage', input_tokens: 10, output_tokens: 5 },
] as unknown as SSEEvent[];

describe('projectSubagentHistory workflow-child backfill', () => {
  it('settles ghost-lane children from the owning run lifecycle', () => {
    const rt = makeRuntime();
    projectSubagentHistory(
      rt,
      new Map([
        [
          'task:wf1',
          {
            messages: [],
            type: 'workflow',
            events: [
              lifecycle({ phase: 'run_started', name: 'briefs', description: 'Fan out' }),
              lifecycle({ phase: 'child_started', seq: 0, label: 'NVDA', subagent_type: 'research', child_task_id: 'ch1' }),
              lifecycle({ phase: 'child_started', seq: 1, label: 'AMD', subagent_type: 'research', child_task_id: 'ch2' }),
              lifecycle({ phase: 'child_done', seq: 0, status: 'ok', child_task_id: 'ch1' }),
              lifecycle({ phase: 'run_completed', status: 'cancelled' }),
            ],
          },
        ],
        ['task:ch1', { messages: [], events: ghostEvents }],
        ['task:ch2', { messages: [], events: ghostEvents }],
      ]),
    );

    const entries = rt.subagentHistory.get().entries;
    expect(entries['task:ch1']).toMatchObject({
      status: 'completed',
      description: 'NVDA',
      type: 'research',
      ownerTaskId: 'task:wf1',
    });
    // Never marked done before the run settled → torn down with the run.
    expect(entries['task:ch2']).toMatchObject({ status: 'cancelled', description: 'AMD' });
  });

  it('leaves children of a still-running run as running', () => {
    const rt = makeRuntime();
    projectSubagentHistory(
      rt,
      new Map([
        [
          'task:wf1',
          {
            messages: [],
            type: 'workflow',
            events: [
              lifecycle({ phase: 'run_started', name: 'briefs' }),
              lifecycle({ phase: 'child_started', seq: 0, label: 'NVDA', subagent_type: 'research', child_task_id: 'ch1' }),
            ],
          },
        ],
        ['task:ch1', { messages: [], events: ghostEvents }],
      ]),
    );

    expect(rt.subagentHistory.get().entries['task:ch1']!.status).toBe('running');
  });

  it('does not override a backend-stamped child status', () => {
    const rt = makeRuntime();
    projectSubagentHistory(
      rt,
      new Map([
        [
          'task:wf1',
          {
            messages: [],
            type: 'workflow',
            events: [
              lifecycle({ phase: 'child_started', seq: 0, label: 'NVDA', child_task_id: 'ch1' }),
              lifecycle({ phase: 'run_completed', status: 'completed' }),
            ],
          },
        ],
        ['task:ch1', { messages: [], events: ghostEvents, status: 'error', error: 'boom' }],
      ]),
    );

    expect(rt.subagentHistory.get().entries['task:ch1']!.status).toBe('error');
  });

  it('backfills a child projected earlier by copy, in one publish', () => {
    const published: SubagentHistorySnapshot[] = [];
    const rt = makeRuntime((s) => published.push(s));
    projectSubagentHistory(rt, new Map([['task:ch1', { messages: [], events: ghostEvents }]]));
    const before = rt.subagentHistory.get();
    const childBefore = before.entries['task:ch1']!;

    projectSubagentHistory(
      rt,
      new Map([
        [
          'task:wf1',
          {
            messages: [],
            type: 'workflow',
            events: [
              lifecycle({ phase: 'child_started', seq: 0, label: 'NVDA', subagent_type: 'research', child_task_id: 'ch1' }),
              lifecycle({ phase: 'child_done', seq: 0, status: 'ok', child_task_id: 'ch1' }),
            ],
          },
        ],
      ]),
    );

    // The run and its child's backfill land together, as one new snapshot.
    expect(published).toHaveLength(2);
    const after = rt.subagentHistory.get();
    expect(after).not.toBe(before);
    expect(after.entries['task:ch1']).toMatchObject({
      description: 'NVDA', type: 'research', ownerTaskId: 'task:wf1', status: 'completed',
    });
    // A reader still holding the earlier snapshot sees it exactly as it was.
    expect(before.entries['task:ch1']).toBe(childBefore);
    expect(childBefore.ownerTaskId).toBeUndefined();
    expect(childBefore.status).toBe('running');
  });
});
