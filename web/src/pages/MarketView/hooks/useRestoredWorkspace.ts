import { useEffect, useRef, useState } from 'react';
import type { Workspace } from '@/types/api';
import { loadPref, savePref } from '../utils/prefs';

interface RestoredWorkspaceOptions {
  /** A workspace the opening link named; it is trusted outright. */
  linkedId: string | null | undefined;
  workspaces: Workspace[];
  isFetchedAfterMount: boolean;
  isSuccess: boolean;
  /** The restored workspace is gone from the list, so the view falls back to its default state. */
  onDropped: () => void;
}

/**
 * The workspace MarketView scopes its requests to. An id restored from storage
 * can name a workspace this account cannot open (deleted since, or saved by
 * someone else on this browser), and every workspace-scoped request made with
 * it comes back 403. So it is withheld as null until the reconcile below has
 * checked it against the list, and the raw id is never handed out.
 */
export function useRestoredWorkspace({
  linkedId, workspaces, isFetchedAfterMount, isSuccess, onDropped,
}: RestoredWorkspaceOptions) {
  const [id, setId] = useState<string | null>(
    () => linkedId || loadPref<string | null>('selectedWorkspaceId', null),
  );
  // The id this mount restored from storage, null when the link named one. It
  // is the only id the reconcile below judges.
  const [restoredId] = useState(() => (linkedId ? null : id));
  // The restored id itself, not a flag: choosing that same id again before the
  // check has run is still a choice of an unchecked id, while any other id
  // releases the hold by no longer matching.
  const [unverifiedId, setUnverifiedId] = useState(restoredId);
  const pending = id !== null && id === unverifiedId;

  useEffect(() => {
    savePref('selectedWorkspaceId', id);
  }, [id]);

  // Reconcile the restored selection against the list, once per mount. If the
  // selected workspace is gone (deleted between visits), drop back to a clean
  // default state (Flash mode + new chat) instead of silently picking a
  // different PTC workspace the user didn't ask for. Resolve both decisions
  // before calling either setter so neither updater runs a side effect on the
  // other piece of state.
  //
  // Gated on a successful fetch that happened during this mount, not merely on
  // data being present: the query has a 30s staleTime, so a remount inside
  // that window is served from cache synchronously and a workspace deleted
  // elsewhere is still in that copy. `isFetchedAfterMount` also flips on an
  // error update, hence `isSuccess`: a failed list load must not read as
  // "your workspace is gone" and clear the selection.
  //
  // Once per mount, deliberately. Absence from this list does not mean
  // deleted: it is one page of 50 ordered by recency, so an idle selection
  // falls off it on its own as other workspaces get activity. Re-running on
  // every refetch would clear the selection mid-session on a window focus, and
  // MarketChatPanel reads a workspace change as a scope change the user made
  // and discards the open thread.
  //
  // A workspace named by the link is trusted outright: the chart tab built
  // that link from a workspace that was open seconds earlier, and one older
  // than the page's fifty is exactly the case this check would misread as
  // deleted, dropping the thread the link carried. So is an id selected since
  // mount, from the selector or a later link: it is the user's choice, and a
  // check that lands after it (the first success after a failed load) must not
  // overrule it.
  //
  // A failed load releases the restored id without judging it: its requests
  // may 403, but a list outage must not leave PTC mode unusable.
  const reconciledRef = useRef(false);
  useEffect(() => {
    if (reconciledRef.current || !isFetchedAfterMount) return;
    setUnverifiedId(null);
    if (!isSuccess) return;
    reconciledRef.current = true;
    if (linkedId || id !== restoredId) return;
    if (id && workspaces.some((ws) => ws.workspace_id === id)) return;
    if (id) onDropped();
    setId(workspaces[0]?.workspace_id ?? null);
  }, [isFetchedAfterMount, isSuccess, workspaces, id, linkedId, restoredId, onDropped]);

  return { selectedWorkspaceId: pending ? null : id, pending, select: setId };
}
