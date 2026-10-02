import type { TurnEndScroll } from '@/lib/turnEndScroll';
import { prefersReducedMotion } from '@/lib/reducedMotion';
import { findTurnReply, useTurnEnd } from './turnEnd';
import type { useChatScroll } from './useChatScroll';
import type { ChatMessage } from '@/types/chat';

type ScrollController = Pick<
  ReturnType<typeof useChatScroll>,
  'scrollAreaRef' | 'getScrollContainer' | 'pinToMessage' | 'pinTargetRef' | 'isNearBottomRef' | 'activeAgentIdRef' | 'entryRestoreSettled'
>;

/**
 * Turn completion (`isStreaming` true -> false, once per turn; an interrupt
 * keeps it true, the caller decides) under the
 * 'reply_start' preference: bring the first line of the final reply, the
 * bubble's last prose block, under the viewport top and stop following. Only a reader the follow was carrying is
 * moved; one who scrolled up keeps their place. pinToMessage decides the rest:
 * a reply shorter than the viewport clamps to the bottom and nothing moves,
 * and its settle window re-measures the anchor as late media lands.
 */
export function useTurnEndScroll(
  scroll: ScrollController,
  {
    messages,
    isStreaming,
    isActiveRef,
    turnEndScroll,
  }: {
    messages: ReadonlyArray<ChatMessage>;
    isStreaming: boolean;
    isActiveRef: { current: boolean };
    turnEndScroll: TurnEndScroll;
  },
) {
  const { scrollAreaRef, getScrollContainer, pinToMessage, pinTargetRef, isNearBottomRef, activeAgentIdRef, entryRestoreSettled } = scroll;
  useTurnEnd(messages, isStreaming, () => {
    if (turnEndScroll !== 'reply_start') return;
    if (activeAgentIdRef.current !== 'main' || !isActiveRef.current) return;
    // A bottom pin is the follow inside its settle window (thread entry, the
    // jump pill) and hands over; an offset or anchor pin holds a place the
    // reader chose.
    if (pinTargetRef.current && pinTargetRef.current.mode !== 'bottom') return;
    if (!isNearBottomRef.current || !entryRestoreSettled()) return;
    const c = getScrollContainer(scrollAreaRef);
    const id = c && findTurnReply(c, messages);
    if (id) pinToMessage(id, prefersReducedMotion() ? 'auto' : 'smooth', true, 'reply');
  });
}
