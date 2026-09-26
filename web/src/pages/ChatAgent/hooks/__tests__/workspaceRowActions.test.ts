import { QueryClient } from '@tanstack/react-query';
import { expect, it } from 'vitest';

import { queryKeys } from '@/lib/queryKeys';
import { invalidateNewWorkspace, invalidateWorkspaceMembership } from '../workspaceRowActions';

it('invalidates workspace and computer projections after membership changes', () => {
  const client = new QueryClient();
  const workspaces = queryKeys.workspaces.lists();
  const computers = queryKeys.computers.lists();
  // Shares the `computers` prefix, and costs a `du` on the machine: a
  // membership change must not re-read it.
  const storage = queryKeys.computers.storage('c1');
  client.setQueryData(workspaces, { workspaces: [] });
  client.setQueryData(computers, { computers: [] });
  client.setQueryData(storage, { live: true, workspaces: [], other_bytes: 0 });

  invalidateWorkspaceMembership(client);

  expect(client.getQueryState(workspaces)?.isInvalidated).toBe(true);
  expect(client.getQueryState(computers)?.isInvalidated).toBe(true);
  expect(client.getQueryState(storage)?.isInvalidated).toBe(false);
  client.clear();
});

it('also refreshes the MCP switches after a workspace is created or duplicated', () => {
  // A new workspace is seeded switched off for servers new workspaces start
  // without, and a duplicate copies its source's switches, built-ins included.
  // The Plugins scope badges read both lists.
  const client = new QueryClient();
  const catalog = queryKeys.mcp.catalog();
  const builtins = queryKeys.mcp.builtins();
  const tools = queryKeys.mcp.workspace('ws-1');
  client.setQueryData(catalog, { servers: [], max_servers: 50 });
  client.setQueryData(builtins, { servers: [] });
  client.setQueryData(tools, { servers: [] });

  invalidateNewWorkspace(client);

  expect(client.getQueryState(catalog)?.isInvalidated).toBe(true);
  expect(client.getQueryState(builtins)?.isInvalidated).toBe(true);
  // Another workspace's own view: nothing there changed.
  expect(client.getQueryState(tools)?.isInvalidated).toBe(false);
  client.clear();
});
