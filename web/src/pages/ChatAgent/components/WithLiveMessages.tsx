import type { ReactNode } from 'react';
import { useLiveMessages, type LiveMessages } from '../session/stream/liveMessages';

/**
 * Renders `children` with the transcript as of the latest streamed chunk. It is
 * a component of its own so that a chunk renders this subtree and not the view
 * that placed it.
 */
export function WithLiveMessages<T>({ store, children }: { store: LiveMessages<T>; children: (messages: T) => ReactNode }) {
  return children(useLiveMessages(store));
}
