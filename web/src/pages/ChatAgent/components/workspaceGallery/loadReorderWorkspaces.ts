import { getAllWorkspaces } from '@/hooks/useAllWorkspaces';
import type { WorkspaceRecord } from './types';

export function getAllReorderWorkspaces(): Promise<WorkspaceRecord[]> {
  return getAllWorkspaces('custom', false);
}
