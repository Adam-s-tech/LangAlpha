/**
 * DispatchStatusProvider — the turn-level batched dispatch-status reader.
 *
 * Many PTCAgentCards in a flash turn register their thread id with one provider;
 * the provider runs a SINGLE getDispatchLiveness query over the sorted id set,
 * distributes each id's status slice, and stops polling once every run is
 * terminal. These tests mock getDispatchLiveness so the batching + distribution
 * + cadence wiring is exercised without a backend.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { renderWithProviders } from '@/test/utils';

vi.mock('../../utils/api', () => ({
  getDispatchLiveness: vi.fn(),
}));

import { getDispatchLiveness } from '../../utils/api';
import { DispatchStatusProvider, useDispatchStatus } from '../usePTCDispatchStatus';

const mockLiveness = getDispatchLiveness as unknown as Mock;

/** Minimal consumer that surfaces a single thread's resolved dispatch status.
 *  `tag` disambiguates the testid when two consumers watch the SAME thread. */
function Probe({ id, tag }: { id: string; tag?: string }) {
  const { status } = useDispatchStatus(id, true);
  return <div data-testid={`probe-${tag ?? id}`}>{status}</div>;
}

/** A rejection shaped like the API client's, which carries the response. */
function httpError(status: number) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status },
  });
}

/** The app's query defaults, one retry for a failed read. The retry is made
 *  at once, so a failed poll is two calls a millisecond apart, and the checks
 *  below land between polls rather than on one. */
function appQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 0, gcTime: 0 } } });
}

/** A turn with one card for thread-a, plus an optional second card on the SAME
 *  thread — the shape a resumed dispatch takes (same id set, same query key). */
function turn(resumed: boolean) {
  return (
    <DispatchStatusProvider>
      <Probe id="thread-a" />
      {resumed && <Probe id="thread-a" tag="resume" />}
    </DispatchStatusProvider>
  );
}

describe('DispatchStatusProvider', () => {
  beforeEach(() => vi.clearAllMocks());

  it('runs ONE batched query for all registered ids and distributes each status', async () => {
    mockLiveness.mockResolvedValue([
      { thread_id: 'thread-a', status: 'active', run_id: 'run-a', can_reconnect: true },
      { thread_id: 'thread-b', status: 'completed', run_id: null, can_reconnect: false },
    ]);

    renderWithProviders(
      <DispatchStatusProvider>
        <Probe id="thread-b" />
        <Probe id="thread-a" />
      </DispatchStatusProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('probe-thread-a')).toHaveTextContent('running'));
    // One request for the whole turn, carrying BOTH ids (sorted) — no per-card
    // fan-out, no second timer.
    expect(mockLiveness).toHaveBeenCalledTimes(1);
    expect(mockLiveness).toHaveBeenCalledWith(['thread-a', 'thread-b']);
    // Each card reads its own slice from the shared result.
    expect(screen.getByTestId('probe-thread-b')).toHaveTextContent('completed');
  });

  it('stops polling once every registered run is terminal', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'completed', run_id: null, can_reconnect: false },
        { thread_id: 'thread-b', status: 'failed', run_id: null, can_reconnect: false },
      ]);
      renderWithProviders(
        <DispatchStatusProvider>
          <Probe id="thread-a" />
          <Probe id="thread-b" />
        </DispatchStatusProvider>,
      );
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
      // All terminal → refetchInterval resolves to false; advancing well past
      // both the fast and steady cadences fires no further requests.
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('wakes the dormant query when a consumer re-registers a terminal thread (resumed dispatch)', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'completed', run_id: null, can_reconnect: false },
      ]);
      const { rerender } = renderWithProviders(turn(false));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('probe-thread-a')).toHaveTextContent('completed');
      // Terminal → the shared query goes dormant.
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);

      // The user resumes a dispatch onto the SAME finished thread: the server
      // is active again and a second card registers the same id — same id set,
      // same query key, so only the wake invalidation can refetch.
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'active', run_id: 'run-a2', can_reconnect: true },
      ]);
      rerender(turn(true));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(2);
      // Both cards flip off the stale terminal slice.
      expect(screen.getByTestId('probe-thread-a')).toHaveTextContent('running');
      expect(screen.getByTestId('probe-resume')).toHaveTextContent('running');
      // Cadence is re-armed: the next poll fires on the fast interval.
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not add a fetch when a consumer re-registers a still-live thread', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'active', run_id: 'run-a', can_reconnect: true },
      ]);
      const { rerender } = renderWithProviders(turn(false));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
      // A second card on a live thread is a plain refcount bump — the wake is
      // terminal-gated, so no extra immediate fetch.
      rerender(turn(true));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
      // Polling simply continues on the existing cadence.
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('grants the full starting-cap budget again after a wake, with fast cadence', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'active', run_id: 'run-a', can_reconnect: true },
      ]);
      const { rerender } = renderWithProviders(turn(false));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      // Accumulate lifetime fetches well past STARTING_POLL_CAP (30) while live.
      for (let i = 0; i < 40; i++) {
        await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      }
      expect(mockLiveness.mock.calls.length).toBeGreaterThan(30);
      // Run finishes → dormant.
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'completed', run_id: null, can_reconnect: false },
      ]);
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      const afterComplete = mockLiveness.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(mockLiveness.mock.calls.length).toBe(afterComplete);

      // Resume: the new run hasn't registered yet, so liveness omits the id
      // and the slice maps to 'starting'.
      mockLiveness.mockResolvedValue([]);
      rerender(turn(true));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const afterWake = mockLiveness.mock.calls.length;
      expect(afterWake).toBe(afterComplete + 1);
      // With lifetime-cumulative counters the starting cap would trip
      // instantly (lifetime polls >= 30) and the cadence would already be
      // steady (10s). The window-scoped counters grant the full budget again
      // AND re-apply the fast 4s cadence relative to the wake.
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(mockLiveness.mock.calls.length).toBe(afterWake + 2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('backs off and then gives up while the liveness read keeps failing', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockRejectedValue(httpError(503));
      renderWithProviders(turn(false), { queryClient: appQueryClient() });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      // A failed read is still a finished poll: the fast window is five polls.
      await act(async () => { await vi.advanceTimersByTimeAsync(18_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(2 * 5);
      // Then the steady cadence, not 4s forever.
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(2 * 11);
      // Never read once, the dispatch shows 'starting' on every round, so the
      // starting cap ends the watch.
      await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(2 * 30);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps a live run on the steady cadence through a failing read', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'active', run_id: 'run-a', can_reconnect: true },
      ]);
      renderWithProviders(turn(false), { queryClient: appQueryClient() });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      mockLiveness.mockRejectedValue(httpError(401));
      await act(async () => { await vi.advanceTimersByTimeAsync(18_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(1 + 2 * 4);
      // The last answer still says running, so a 401 (a session that can come
      // back) neither stops the poll nor keeps it fast.
      await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(1 + 2 * 64);
      expect(screen.getByTestId('probe-thread-a')).toHaveTextContent('running');
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops polling on a refusal that asking again will not change', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockRejectedValue(httpError(403));
      renderWithProviders(turn(false), { queryClient: appQueryClient() });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps polling while a run is still live', async () => {
    vi.useFakeTimers();
    try {
      mockLiveness.mockResolvedValue([
        { thread_id: 'thread-a', status: 'active', run_id: 'run-a', can_reconnect: true },
      ]);
      renderWithProviders(
        <DispatchStatusProvider>
          <Probe id="thread-a" />
        </DispatchStatusProvider>,
      );
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(mockLiveness).toHaveBeenCalledTimes(1);
      // A live run polls again on the fast cadence (4s).
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(mockLiveness).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
