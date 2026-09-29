import type { ContentSegmentRecord } from './types';

/** The reply's prose in transcript order. Segments can land out of order, so they are sorted by `order` the way the bubble renders them; a message with no text segments falls back to its flat `content`. Shared by copy, the minimap preview and the deliverables deck so all three read the same text. `max` stops reading once that much text is in hand, for a caller that shows only the head of a reply still streaming. */
export function assistantText(message: { contentSegments?: unknown; content?: unknown }, max = Infinity): string {
  const segments = message.contentSegments as ContentSegmentRecord[] | undefined;
  let text = '';
  if (segments) {
    const texts = segments.filter((s) => s.type === 'text').sort((a, b) => a.order - b.order);
    for (const s of texts) {
      if (text.length >= max) break;
      text += (s.content ?? '').slice(0, max - text.length);
    }
  }
  return text || (typeof message.content === 'string' ? message.content.slice(0, max) : '');
}
