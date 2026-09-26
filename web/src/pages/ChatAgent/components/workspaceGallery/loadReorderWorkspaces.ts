import { getAllWorkspaces } from '@/hooks/useWorkspaces';
import type { WorkspaceRecord } from './types';

export function getAllReorderWorkspaces(): Promise<WorkspaceRecord[]> {
  return getAllWorkspaces('custom', false);
}
