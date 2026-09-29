import type { ContentSegmentRecord } from './types';

/** Segments in `order`, sorted only when they are not already: a streaming reply is one segment per chunk and arrives in order, so the common case is a single pass rather than a copy and a sort on every chunk. */
export function inOrder<T extends { order: number }>(segments: readonly T[]): readonly T[] {
  for (let i = 1; i < segments.length; i++) {
    if (segments[i].order < segments[i - 1].order) return [...segments].sort((a, b) => a.order - b.order);
  }
  return segments;
}

/** The reply's prose in transcript order. Segments can land out of order, so they are sorted by `order` the way the bubble renders them; a message with no text segments falls back to its flat `content`. Shared by copy, the minimap preview and the deliverables deck so all three read the same text. `max` stops reading once that much text is in hand, for a caller that shows only the head of a reply still streaming. */
export function assistantText(message: { contentSegments?: unknown; content?: unknown }, max = Infinity): string {
  const segments = message.contentSegments as ContentSegmentRecord[] | undefined;
  let text = '';
  if (segments) {
    for (const s of inOrder(segments)) {
      if (text.length >= max) break;
      if (s.type === 'text') text += (s.content ?? '').slice(0, max - text.length);
    }
  }
  return text || (typeof message.content === 'string' ? message.content.slice(0, max) : '');
}
