import { lazy, type ComponentProps, type ComponentType, type LazyExoticComponent } from 'react';

/**
 * `React.lazy` for a component that eager code renders: the seam where a
 * subtree first reaches framer-motion. framer is off the entry chunk, so no
 * `MotionConfig` can sit at the app root; each seam wraps what it loads in
 * `reducedMotion="user"` instead, which collapses transform and layout
 * animations to instant for a reader who asked the OS for less motion (opacity
 * still animates). Context reaches portals, so one wrapper covers everything
 * the subtree opens. framer loads beside the chunk, never after it.
 *
 * An eager `React.lazy` that skips this helper leaves its motion ignoring the
 * reader's setting, silently.
 */
export function lazyWithMotion<
  // React.lazy's own constraint: `any` is what admits every props type.
  T extends ComponentType<any>,
>(
  load: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return lazy(async () => {
    const [{ default: Component }, MotionConfig] = await Promise.all([
      load(),
      // Destructured right in the `.then`, the shape the bundler reads as
      // "only MotionConfig". Handed the whole namespace, it re-exports all of
      // framer from a chunk of its own, 11 kB gz on every route.
      import('./framer').then(({ MotionConfig }) => MotionConfig),
    ]);
    function WithMotion(props: ComponentProps<T>) {
      return (
        <MotionConfig reducedMotion="user">
          <Component {...props} />
        </MotionConfig>
      );
    }
    // Props pass through untouched, so the wrapper keeps T's contract.
    return { default: WithMotion as unknown as T };
  });
}
