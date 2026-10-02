import type { TurnEndScroll } from '@/lib/turnEndScroll';
import { prefersReducedMotion } from '@/lib/reducedMotion';
import type { useChatMessages } from '../../hooks/useChatMessages';
import type { StreamFollowControls } from './streamFollow';
import { findTurnReply, useTurnEnd } from './turnEnd';

type ChatHandle = ReturnType<typeof useChatMessages>;
type TurnStarters = Pick<ChatHandle, 'handleSendMessage' | 'handleEditMessage' | 'handleRegenerate' | 'handleRetry'>;
type TurnState = Pick<ChatHandle, 'messages' | 'isLoading' | 'pendingInterrupt' | 'pendingRejection'>;

/** A pending interrupt or plan feedback clears isLoading, but the turn is still
 *  open: the reply resumes once the reader answers, so the follow keeps its
 *  claim and the turn-end landing waits. */
export function isTurnOpen({ isLoading, pendingInterrupt, pendingRejection }: TurnState): boolean {
  return isLoading || !!pendingInterrupt || !!pendingRejection;
}

/**
 * The follow policy of both chat surfaces, each over its own scroll engine. A
 * turn the reader starts (a send or steer, an edit, a regenerate or retry)
 * takes them to the end wherever they had scrolled, since their message and
 * the reply land there. A finished turn lands where the "When a reply
 * finishes" preference says. Returns the chat's turn starters with the rejoin
 * built in, so a new send site cannot leave it out. A turn that resumes on its
 * own (an answered approval, a reconnect) is not the reader's to follow.
 */
export function useTranscriptFollow(
  chat: TurnStarters & TurnState,
  follow: StreamFollowControls,
  getScroller: () => HTMLElement | null,
  turnEndScroll: TurnEndScroll,
): TurnStarters {
  const { messages } = chat;
  useTurnEnd(messages, isTurnOpen(chat), () => {
    if (turnEndScroll !== 'reply_start') return;
    const c = getScroller();
    const id = c && findTurnReply(c, messages);
    if (id) follow.landOnReply(id, prefersReducedMotion() ? 'auto' : 'smooth');
  });
  const rejoining = <A extends unknown[], R>(start: (...args: A) => R) => (...args: A): R => {
    const started = start(...args);
    follow.rejoin();
    return started;
  };
  return {
    handleSendMessage: rejoining(chat.handleSendMessage),
    handleEditMessage: rejoining(chat.handleEditMessage),
    handleRegenerate: rejoining(chat.handleRegenerate),
    handleRetry: rejoining(chat.handleRetry),
  };
}
