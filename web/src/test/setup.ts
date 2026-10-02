/// <reference types="vitest/globals" />
import '@testing-library/jest-dom/vitest';
import { initI18n } from '@/i18n';
import enUS from '@/locales/en-US.json';
import zhCN from '@/locales/zh-CN.json';

// The app's i18n config with both catalogs handed over up front, so `t()`
// returns real strings and a language switch lands before the next line. The
// app loads catalogs on demand, which would make every switch in a test async.
void initI18n({ 'en-US': { translation: enUS }, 'zh-CN': { translation: zhCN } });

// Mock window.matchMedia for framer-motion
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// Mock IntersectionObserver
class IntersectionObserverMock {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
window.IntersectionObserver = IntersectionObserverMock as unknown as typeof IntersectionObserver;

// Mock ResizeObserver
class ResizeObserverMock {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
window.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;
