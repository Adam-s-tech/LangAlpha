/**
 * The spelling of a MarketView URL. `buildMarketViewUrl` writes it for the
 * chat's chart tab, the annotation card and the dashboard header;
 * `readMarketViewRoute` reads it back for `MarketView.tsx` and
 * `MarketChatPanel.tsx`, so the two sides share one set of param names.
 * Older Dashboard call sites still spell `/market?symbol=` by hand.
 */
export interface MarketViewRoute {
  symbol: string;
  timeframe?: string;
  /** The PTC workspace whose chat should continue beside the chart. Naming one
   *  also selects PTC mode, since that mode is what the workspace belongs to. */
  workspaceId?: string | null;
  threadId?: string | null;
  /** Where "Return to chat" goes: a path inside the app, or the reader drops it. */
  returnTo?: string | null;
}

export type MarketViewMode = 'ptc' | 'fast';

/** What a MarketView URL carries, each field null when the param is absent. */
export interface MarketViewRouteParams {
  symbol: string | null;
  timeframe: string | null;
  workspaceId: string | null;
  mode: MarketViewMode | null;
  threadId: string | null;
  returnTo: string | null;
}

/** The thread id that means "no particular thread"; the URL never carries it. */
export const DEFAULT_MARKET_THREAD = '__default__';

const PARAM = {
  symbol: 'symbol',
  timeframe: 'tf',
  mode: 'mode',
  workspaceId: 'ws',
  threadId: 'thread',
  returnTo: 'returnTo',
} as const;

/** Every param the route owns, for a reader that clears them once consumed. */
export const MARKET_VIEW_ROUTE_PARAMS: readonly string[] = Object.values(PARAM);

export function buildMarketViewUrl({ symbol, timeframe, workspaceId, threadId, returnTo }: MarketViewRoute): string {
  const sp = new URLSearchParams();
  sp.set(PARAM.symbol, symbol);
  if (timeframe) sp.set(PARAM.timeframe, timeframe);
  if (workspaceId) {
    sp.set(PARAM.mode, 'ptc');
    sp.set(PARAM.workspaceId, workspaceId);
  }
  if (threadId && threadId !== DEFAULT_MARKET_THREAD) sp.set(PARAM.threadId, threadId);
  if (returnTo) sp.set(PARAM.returnTo, returnTo);
  return `/market?${sp.toString()}`;
}

/** Stands in for the app's origin, so the check below needs no `window`. */
const IN_APP_BASE = 'http://in-app.invalid';

/**
 * `returnTo` as a path inside this app, or null for anything that would leave it.
 *
 * The param arrives in a URL anyone can craft, and "Return to chat" navigates to
 * it, so it is an open redirect unless it resolves to the same origin. The
 * resolve catches schemes, hosts, and the tabs and newlines a browser strips.
 * The path it yields is checked after it, because dot segments collapse during
 * the resolve: `/..//host` comes out as `//host`, which a browser reads as
 * another host, as it does `/\host`. A consumer that decodes once would do the
 * same with `/%2F%2Fhost`.
 */
function inAppPath(raw: string | null): string | null {
  if (!raw || !raw.startsWith('/')) return null;
  let url: URL;
  try {
    url = new URL(raw, IN_APP_BASE);
  } catch {
    return null;
  }
  if (url.origin !== IN_APP_BASE) return null;
  const path = url.pathname + url.search + url.hash;
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return null;
  }
  return /^\/[\\/]/.test(path) || /^\/[\\/]/.test(decoded) ? null : path;
}

export function readMarketViewRoute(searchParams: URLSearchParams): MarketViewRouteParams {
  const modeParam = searchParams.get(PARAM.mode);
  return {
    symbol: searchParams.get(PARAM.symbol),
    timeframe: searchParams.get(PARAM.timeframe),
    workspaceId: searchParams.get(PARAM.workspaceId),
    mode: modeParam === 'ptc' || modeParam === 'fast' ? modeParam : null,
    threadId: searchParams.get(PARAM.threadId),
    returnTo: inAppPath(searchParams.get(PARAM.returnTo)),
  };
}
