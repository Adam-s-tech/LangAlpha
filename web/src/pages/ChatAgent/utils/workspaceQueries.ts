/**
 * The workspace detail query, defined once so the hook that renders a
 * workspace and the warm path that writes its status agree on the fetch and
 * the type of what the cache holds.
 */
import { queryOptions } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { getWorkspace } from './api';

export function workspaceDetailQuery(workspaceId: string) {
  return queryOptions({
    queryKey: queryKeys.workspaces.detail(workspaceId),
    queryFn: () => getWorkspace(workspaceId),
    staleTime: 5 * 60_000,
  });
}
