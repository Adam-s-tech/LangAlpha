/// <reference types="vitest/globals" />
import i18n, { initI18n } from '@/i18n';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';

// Setup for the `node` project in vitest.config.ts: test files that need no DOM.
//
// A file only belongs there if it never touches the browser. The dangerous
// failure is not a crash but a quiet pass: code that probes `typeof window`
// takes its no-DOM branch here, a branch production never runs, and the
// assertions still hold. So every browser global below is a trap. Reading one
// from app or test code throws where it happens and, in case something catches
// that, fails the file again in `afterAll`. The fix is to take the file out of
// the node project's list, not to guard the read.

// jsdom answers `navigator.language` with en-US; Node derives it from the
// machine's locale. Pin it so a node-project file renders the same strings on
// every machine. Done before the traps so i18n's own document probe still runs.
// Both catalogs up front, as setup.ts does, so the on-demand loader never runs.
await initI18n({ 'en-US': { translation: enUS }, 'zh-CN': { translation: zhCN } });
await i18n.changeLanguage('en-US');

const BROWSER_GLOBALS = [
  'window',
  'document',
  'navigator',
  'location',
  'localStorage',
  'sessionStorage',
  'matchMedia',
  'getComputedStyle',
  'requestAnimationFrame',
  'customElements',
  'HTMLElement',
  'Element',
] as const;

// Vitest's own formatters and fake timers probe `window` and friends to decide
// how to print or what to patch. Those reads are the runner's, not the test's.
const RUNNER_FRAME = /[\\/]node_modules[\\/](?:vitest|@vitest[\\/][^\\/]+|chai|tinyrainbow|loupe)[\\/]|\(node:|at node:/;

const touched = new Map<string, string>();

function callerFrame(): string {
  const frames = (new Error().stack ?? '').split('\n').slice(1);
  return frames.find((f) => !f.includes('setup.node.ts'))?.trim() ?? '<unknown>';
}

for (const name of BROWSER_GLOBALS) {
  const native = Object.getOwnPropertyDescriptor(globalThis, name);
  const read = (): unknown =>
    native && ('get' in native && native.get ? native.get.call(globalThis) : native.value);
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get() {
      const frame = callerFrame();
      if (RUNNER_FRAME.test(frame)) return read();
      if (!touched.has(name)) touched.set(name, frame);
      throw new ReferenceError(
        `${name} read in a node-environment test (${frame}). ` +
          'This file uses the browser: remove it from the node project in vitest.config.ts.',
      );
    },
  });
}

afterAll(() => {
  if (touched.size === 0) return;
  const reads = [...touched].map(([name, frame]) => `  ${name}: ${frame}`).join('\n');
  throw new Error(
    `Browser globals were read in a node-environment test file:\n${reads}\n` +
      'Remove the file from the node project in vitest.config.ts so it runs under jsdom.',
  );
});
