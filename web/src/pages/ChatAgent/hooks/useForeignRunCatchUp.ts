import { useEffect, useRef, useState } from 'react';
import { useThreadFeedRunId } from '@/lib/threadLifecycle/store';

interface ForeignRunCatchUpOptions {
  threadId: string;
  isActive: boolean;
  /** The view is streaming or loading its history. */
  busy: boolean;
  awaitingReportBack: boolean;
  isOwnRun: (runId: string) => boolean;
  /** Resolves false to be asked again, e.g. when it could not read the thread's status. */
  catchUp: () => Promise<boolean>;
}

const RETRY_FIRST_MS = 2_000;
const RETRY_MAX_MS = 30_000;

/**
 * Brings a view in line with runs it did not start. Cached views stay mounted
 * while hidden (useChatViewCache), so one shown again checks for a run that
 * started or ended meanwhile; mounting is not a reason, since the first load
 * reads the thread itself. A run that starts on this thread from somewhere
 * else while the view is shown (another tab, an automation that waited for the
 * last turn) is announced on the user feed and checked the same way. Nothing
 * is checked while the view is busy: a waiting automation starts the moment
 * the turn settles, often before this view's stream has closed, and a check
 * landing inside a send, an edit or a load would reload over it. The view's
 * own run is announced too and can end before its announcement lands, when the
 * turn watermark (only a lower bound) could take the view for stale and reload
 * it for nothing, so a run this view streamed is passed over. So is a run
 * while the report-back watch is armed, since that watch attaches the thread's
 * report-back runs itself and a reload would race it.
 *
 * A check that could not read the thread's status stays owed, retried with
 * backoff while the view is shown on the thread: the failure reads as nothing
 * running, which would leave the run off screen until a reload. Nothing runs
 * it before the backoff is up. The load a check asks for turns the view busy
 * and then idle, and checking again on that edge would reload back to back.
 */
export function useForeignRunCatchUp({
  threadId,
  isActive,
  busy,
  awaitingReportBack,
  isOwnRun,
  catchUp,
}: ForeignRunCatchUpOptions): void {
  const feedRunId = useThreadFeedRunId(threadId);
  const seenRef = useRef({ threadId, runId: feedRunId, isActive });
  // The checks owed: one whatever the feed says (the view was shown again, or a
  // check failed), and one for the run the feed announced, which is passed over
  // when it is the view's own.
  const owedRef = useRef<{ check: boolean; runId: string | null }>({ check: false, runId: null });
  const inFlightRef = useRef(false);
  const retryRef = useRef<{ timer?: ReturnType<typeof setTimeout>; delay: number; stopped: boolean; waiting: boolean }>({
    delay: RETRY_FIRST_MS,
    stopped: false,
    waiting: false,
  });
  const [recheck, setRecheck] = useState(0);

  useEffect(() => {
    const retry = retryRef.current;
    retry.stopped = false;
    return () => {
      retry.stopped = true;
      clearTimeout(retry.timer);
    };
  }, []);

  useEffect(() => {
    const seen = seenRef.current;
    seenRef.current = { threadId, runId: feedRunId, isActive };
    const owed = owedRef.current;
    const retry = retryRef.current;
    if (seen.threadId !== threadId || !isActive) {
      // Hidden, or moved to another thread: what was owed was the old thread's,
      // and a hidden view checks when it is shown again.
      owed.check = false;
      owed.runId = null;
      clearTimeout(retry.timer);
      retry.waiting = false;
      retry.delay = RETRY_FIRST_MS;
    }
    if (isActive && !seen.isActive) owed.check = true;
    if (isActive && seen.threadId === threadId && feedRunId && feedRunId !== seen.runId) owed.runId = feedRunId;
    if (inFlightRef.current || busy || retry.waiting) return;
    if (owed.runId && (awaitingReportBack || isOwnRun(owed.runId))) owed.runId = null;
    if (!owed.check && !owed.runId) return;

    // One check settles both.
    owed.check = false;
    owed.runId = null;
    clearTimeout(retry.timer);
    inFlightRef.current = true;
    void Promise.resolve()
      .then(catchUp)
      .catch((err: unknown) => {
        // A throw past the status read will throw again, so asking again won't help.
        console.error('[useForeignRunCatchUp] catch-up failed:', err);
        return true;
      })
      .then((read) => {
        inFlightRef.current = false;
        if (retry.stopped) return;
        const now = seenRef.current;
        // A failed read stays owed while the view is still shown on its thread,
        // whatever the feed announces next. It already passed the run filter, and
        // the view's own run, announced during the backoff, would otherwise take
        // its place and be passed over.
        const failed = !read && now.isActive && now.threadId === threadId;
        if (failed) owed.check = true;
        else retry.delay = RETRY_FIRST_MS;
        if (!owed.check && !owed.runId) return;
        // Asked again while this check ran, or it failed and waits out the backoff.
        retry.waiting = failed;
        retry.timer = setTimeout(() => {
          retry.waiting = false;
          setRecheck((n) => n + 1);
        }, failed ? retry.delay : 0);
        if (failed) retry.delay = Math.min(retry.delay * 2, RETRY_MAX_MS);
      });
  }, [threadId, feedRunId, isActive, busy, awaitingReportBack, isOwnRun, catchUp, recheck]);
}
