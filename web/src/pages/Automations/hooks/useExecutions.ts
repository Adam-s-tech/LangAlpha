import { useCallback } from 'react';
import { queryOptions, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import type { AutomationExecution } from '@/types/automation';
import { isRunLive } from '../utils/status';
import { pollMs } from '../utils/polling';
import { listExecutions } from '../utils/api';

interface UseExecutionsResult {
  executions: AutomationExecution[];
  loading: boolean;
}

const EMPTY: AutomationExecution[] = [];

const executionsQuery = (automationId: string) =>
  queryOptions({
    queryKey: queryKeys.automations.executions(automationId),
    queryFn: async () => (await listExecutions(automationId, { limit: 20, offset: 0 })).data,
    staleTime: 5000,
  });

export function useExecutions(automationId: string): UseExecutionsResult {
  const { data, isLoading } = useQuery({
    ...executionsQuery(automationId),
    refetchInterval: (query) => pollMs(!!query.state.data?.executions.some((e) => isRunLive(e.status))),
    refetchIntervalInBackground: false,
  });

  return { executions: data?.executions ?? EMPTY, loading: isLoading };
}

/** Warms an automation's runs before it is opened, so its pane renders whole
 *  instead of swapping a skeleton for the table a beat after arriving. */
export function usePrefetchExecutions() {
  const queryClient = useQueryClient();
  return useCallback(
    (automationId: string) => {
      void queryClient.prefetchQuery(executionsQuery(automationId));
    },
    [queryClient],
  );
}
