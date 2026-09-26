/**
 * The app's one import of framer-motion; everything that animates imports it
 * from here (ESLint `no-restricted-imports` holds the line). framer stays off
 * the entry chunk, so there is no app-root setup that could run first, and this
 * module is the only place guaranteed to evaluate before any motion code does.
 *
 * What it sets up: every framer animation is instant while the tab is hidden.
 * A hidden tab runs no animation frames, and framer schedules everything, even
 * an instant animation, on its frame loop. So an animation created while the
 * tab is away sits at its first keyframe until the tab returns, then starts its
 * clock from that frame and plays in full. A streaming transcript accumulates
 * dozens of them (rows folding out of the live zone at height 0, new rows
 * unfolding from it, the accordion growing), and the return becomes a burst of
 * collapses and expansions layered over a scroll pin that already landed.
 * Marking the animations instant at creation makes the first painted frame
 * after the return the settled layout. An animation already in flight at hide
 * time needs nothing: its first tick back reads the wall clock and finishes it
 * in that same frame.
 *
 * Reduced motion is the other app-wide rule and it cannot live here: it is a
 * context value, so each lazily loaded root that renders motion wraps itself in
 * `<MotionConfig reducedMotion="user">` (see lib/lazyWithMotion.tsx).
 */
import { MotionGlobalConfig } from 'framer-motion';

function apply(): void {
  MotionGlobalConfig.skipAnimations = document.hidden;
}

if (typeof document !== 'undefined') {
  apply();
  document.addEventListener('visibilitychange', apply);
}

export * from 'framer-motion';
