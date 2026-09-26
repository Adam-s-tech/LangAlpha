import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import type { Workspace } from '@/types/api';
import { getWorkspaces } from '../pages/ChatAgent/utils/api';

interface UseWorkspacesOptions {
  limit?: number;
  offset?: number;
  sortBy?: string;
  includeFlash?: boolean;
  enabled?: boolean;
  /** 'always' forces a fetch on mount even inside the staleTime window, for a
   *  consumer that has to decide something against the current list. */
  refetchOnMount?: boolean | 'always';
}

/**
 * Shared hook for workspace list queries.
 * Uses keepPreviousData for smooth pagination transitions.
 * All consumers with the same params share one cached entry.
 */
export function useWorkspaces({ limit = 20, offset = 0, sortBy = 'custom', includeFlash = false, enabled = true, refetchOnMount }: UseWorkspacesOptions = {}) {
  const params = { limit, offset, sortBy, includeFlash };
  return useQuery({
    queryKey: queryKeys.workspaces.list(params),
    queryFn: () => getWorkspaces(limit, offset, sortBy, includeFlash),
    enabled,
    staleTime: 30_000,
    refetchOnMount,
    placeholderData: keepPreviousData,
  });
}

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
export function useAllWorkspaces({ sortBy = 'custom', includeFlash = false }: Pick<UseWorkspacesOptions, 'sortBy' | 'includeFlash'> = {}) {
  return useQuery({
    queryKey: queryKeys.workspaces.list({ view: 'all', sortBy, includeFlash }),
    queryFn: async () => {
      const workspaces = await getAllWorkspaces(sortBy, includeFlash);
      return { workspaces, total: workspaces.length };
    },
    staleTime: 30_000,
  });
}
