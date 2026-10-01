import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import type { Workspace } from '@/types/api';
import { getWorkspaces } from '../pages/ChatAgent/utils/api';

// Apart from useWorkspaces.ts because the entry imports that module, and only
// lazy pages list every workspace: code here would ride every first paint.

// The list endpoint's cap on one page.
const WORKSPACE_PAGE_SIZE = 100;

/** Every workspace, one capped page at a time. */
export async function getAllWorkspaces(
  sortBy: string,
  includeFlash: boolean,
): Promise<Workspace[]> {
  const rows: Workspace[] = [];

  while (true) {
    const page = await getWorkspaces(
      WORKSPACE_PAGE_SIZE,
      rows.length,
      sortBy,
      includeFlash,
    );
    rows.push(...page.workspaces);
    const reachedTotal = typeof page.total === 'number' && rows.length >= page.total;
    if (reachedTotal || page.workspaces.length < WORKSPACE_PAGE_SIZE) return rows;
  }
}

/**
 * Every workspace, page by page, for a surface that lists or counts all of
 * them: one page stops at 100, and a count read off a truncated list is wrong
 * without looking wrong. Cached in the list shape so an optimistic row patch
 * reaches it too.
 */
export function useAllWorkspaces({ sortBy = 'custom', includeFlash = false }: { sortBy?: string; includeFlash?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.workspaces.list({ view: 'all', sortBy, includeFlash }),
    queryFn: async () => {
      const workspaces = await getAllWorkspaces(sortBy, includeFlash);
      return { workspaces, total: workspaces.length };
    },
    staleTime: 30_000,
  });
}
