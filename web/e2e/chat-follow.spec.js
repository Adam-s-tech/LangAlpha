/**
 * The main transcript's streaming follow, at the one moment a test can stage
 * but a reader only meets by chance: between a follow and that follow's own
 * scroll event, which arrives a frame later.
 *
 * Sending pins the view to the bottom until the pin's 8 s hard cap, and only
 * then does the follow take over, so each case waits past the cap before it
 * intercepts the next follow.
 */
import { configureSSE, resetMockServer, mockAPI, test, expect } from './fixtures.js';
import { sseEvents } from './helpers/mockResponses.js';
import { TH, chatViewOverrides } from './helpers/chatScenario.js';
import { buildEvents } from './perf/streamFixture.js';

// SETTLE_HARD_CAP_MS in useChatScroll.ts.
const PIN_HARD_CAP_MS = 8000;

/** Runs `onFollow` once, right after the first follow that moves at least `minMove` px. */
async function interceptFollow(page, minMove, onFollow) {
  await page.evaluate(([minMove, body]) => {
    const vp = document.querySelector('[data-message-id]').closest('.overflow-auto');
    const scrollTo = vp.scrollTo;
    const act = new Function('vp', 'top', body);
    window.__intercepted = false;
    vp.scrollTo = function (arg) {
      const before = this.scrollTop;
      scrollTo.apply(this, arguments);
      // A pin targets scrollHeight; only the follow targets the exact bottom.
      const follow = typeof arg === 'object' && arg.top === this.scrollHeight - this.clientHeight;
      if (window.__intercepted || !follow || arg.top - before < minMove) return;
      window.__intercepted = true;
      act(this, arg.top);
    };
  }, [minMove, onFollow]);
  await page.waitForFunction(() => window.__intercepted, null, { timeout: 8000 });
}

function view(page) {
  return page.evaluate(() => {
    const vp = document.querySelector('[data-message-id]').closest('.overflow-auto');
    return { top: vp.scrollTop, fromBottom: vp.scrollHeight - vp.scrollTop - vp.clientHeight };
  });
}

test.describe('streaming follow', () => {
  test.beforeEach(async ({ page }) => {
    await resetMockServer();
    await mockAPI(page, chatViewOverrides());
    await configureSSE({ method: 'GET', path: `/api/v1/threads/${TH}/messages/replay`, events: [sseEvents.replayDone()], delay: 10 });
    await configureSSE({ method: 'POST', path: `/api/v1/threads/${TH}/messages`, events: buildEvents(8, { sections: 12 }), delay: 20 });
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto(`/chat/t/${TH}`);
    await page.waitForSelector('textarea', { timeout: 10000 });
    await page.locator('textarea').fill('Give me an earnings deep dive on NVDA');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(PIN_HARD_CAP_MS + 1000);
  });

  test('keeps following when a tall block lands before the follow is seen', async ({ page }) => {
    // The follow's scroll event then finds the bottom 600 px away, further than
    // the band, though the reader never moved.
    await interceptFollow(page, 0, `
      const block = document.createElement('div');
      block.style.height = '600px';
      vp.querySelector('.max-w-3xl').appendChild(block);
    `);
    await expect.poll(async () => (await view(page)).fromBottom).toBeLessThan(5);
    await expect(page.getByRole('button', { name: 'Scroll to latest message' })).toBeHidden();
  });

  test('lets a reader leave by a notch taken before the follow is seen', async ({ page }) => {
    // A notch smaller than the growth the follow covered: measured from where
    // the view was before the follow, it reads as a move down.
    await interceptFollow(page, 30, 'vp.scrollTop = top - 12; window.__nudgedTop = vp.scrollTop;');
    await page.waitForTimeout(1000);
    const nudged = await page.evaluate(() => window.__nudgedTop);
    const after = await view(page);
    expect(Math.abs(after.top - nudged)).toBeLessThan(2);
    expect(after.fromBottom).toBeGreaterThan(12);
    await expect(page.getByRole('button', { name: 'Scroll to latest message' })).toBeVisible();
  });
});
