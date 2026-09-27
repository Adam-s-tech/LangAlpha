import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

const TEST_FILES = ['src/**/*.test.{ts,tsx}'];
const EXCLUDE = ['e2e/**', 'node_modules/**'];

// Test files that need no DOM, run without jsdom. A file joins this list only
// once it passes here: src/test/setup.node.ts turns every browser global into a
// trap, so a file that reads one fails rather than passing on a no-DOM branch
// production never takes. New files default to jsdom.
const NODE_FILES = [
  'src/__tests__/e2eImportGraph.test.ts',
  'src/__tests__/sessionReadDiscipline.test.ts',
  'src/components/ui/__tests__/cardDeck.test.ts',
  'src/components/ui/__tests__/chat-input.contextBlocks.test.ts',
  'src/components/ui/__tests__/chat-input.modelPick.test.ts',
  'src/components/ui/__tests__/chat-input.quickAccess.test.ts',
  'src/components/ui/__tests__/chat-input.toolbarFold.test.ts',
  'src/components/ui/__tests__/use-toast.pinned.test.ts',
  'src/lib/__tests__/authErrors.test.ts',
  'src/lib/__tests__/authFetch.test.ts',
  'src/lib/__tests__/authToken.test.ts',
  'src/lib/__tests__/desktopAuthHandoff.test.ts',
  'src/lib/__tests__/marketUtils.test.ts',
  'src/lib/__tests__/modelPreferences.test.ts',
  'src/lib/__tests__/modelTuning.test.ts',
  'src/lib/__tests__/queryKeys.test.ts',
  'src/lib/__tests__/randomUUID.test.ts',
  'src/lib/__tests__/secretNames.test.ts',
  'src/lib/__tests__/timezones.test.ts',
  'src/lib/__tests__/utils.test.ts',
  'src/lib/bars/__tests__/barsClient.test.ts',
  'src/lib/bars/__tests__/currencyDisplay.test.ts',
  'src/lib/bars/__tests__/exchanges.test.ts',
  'src/lib/bars/__tests__/fetchBarsDelta.test.ts',
  'src/lib/bars/__tests__/formingBar.test.ts',
  'src/lib/bars/__tests__/legacyBars.test.ts',
  'src/lib/bars/__tests__/marketSession.test.ts',
  'src/lib/bars/__tests__/rangePresets.test.ts',
  'src/locales/__tests__/keys.test.ts',
  'src/pages/Automations/utils/__tests__/feed.test.ts',
  'src/pages/Automations/utils/__tests__/form.test.ts',
  'src/pages/Automations/utils/__tests__/moments.test.ts',
  'src/pages/Automations/utils/__tests__/schedule.test.ts',
  'src/pages/Automations/utils/__tests__/status.test.ts',
  'src/pages/Automations/utils/__tests__/timeOfDay.test.ts',
  'src/pages/ChatAgent/components/__tests__/ChatView.routing.test.tsx',
  'src/pages/ChatAgent/components/__tests__/chartAnnotationGrouping.test.ts',
  'src/pages/ChatAgent/components/__tests__/extractLeadingBoldHeader.test.ts',
  'src/pages/ChatAgent/components/__tests__/minimapEntries.test.ts',
  'src/pages/ChatAgent/components/filePanel/__tests__/fileMeta.test.ts',
  'src/pages/ChatAgent/components/mcp/__tests__/mcpHeaderNames.test.ts',
  'src/pages/ChatAgent/components/mcp/__tests__/mcpHeadersFingerprint.test.ts',
  'src/pages/ChatAgent/components/mcp/__tests__/mcpState.test.ts',
  'src/pages/ChatAgent/components/messageList/__tests__/normalizeSubagentText.test.ts',
  'src/pages/ChatAgent/components/viewers/excel/__tests__/precedents.test.ts',
  'src/pages/ChatAgent/components/viewers/excel/__tests__/snippet.test.ts',
  'src/pages/ChatAgent/components/viewers/html/__tests__/wsfilesUrl.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/historyEventHandlers.stopped.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/historyEventHandlers.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/htmlWidgetHandlers.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/messageHelpers.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/requestKey.test.ts',
  'src/pages/ChatAgent/hooks/utils/__tests__/streamEventHandlers.taskResult.test.ts',
  'src/pages/ChatAgent/session/__tests__/launchReplyParity.test.ts',
  'src/pages/ChatAgent/session/__tests__/marketWatchEvents.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.batchDecisions.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.batchVerdict.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.claimedBatch.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.claimedInterrupts.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.creditPause.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.orderDecisions.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.taskStopReason.test.ts',
  'src/pages/ChatAgent/session/history/__tests__/replayHistory.toolApprovalReraise.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/answerBoard.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/fromLiveEvent.creditPause.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/orderApproval.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/planInterruptRouting.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/stripHistoryInterruptCards.test.ts',
  'src/pages/ChatAgent/session/interrupts/__tests__/toolApproval.test.ts',
  'src/pages/ChatAgent/session/stream/__tests__/finalizeMessage.test.ts',
  'src/pages/ChatAgent/session/stream/__tests__/reconnectStamps.test.ts',
  'src/pages/ChatAgent/session/stream/__tests__/steeringRollback.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/projectHistory.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/resolveSubagentTelemetry.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/settleWorkflowRunFromClosure.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/subagentMetrics.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/subagentStatus.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/taskSegmentBuilder.test.ts',
  'src/pages/ChatAgent/session/subagents/__tests__/workflowRunState.test.ts',
  'src/pages/ChatAgent/utils/__tests__/a1.test.ts',
  'src/pages/ChatAgent/utils/__tests__/agentId.test.ts',
  'src/pages/ChatAgent/utils/__tests__/agentPaths.test.ts',
  'src/pages/ChatAgent/utils/__tests__/chartSelectionToContext.test.ts',
  'src/pages/ChatAgent/utils/__tests__/compactionControl.test.ts',
  'src/pages/ChatAgent/utils/__tests__/fileArtifact.test.ts',
  'src/pages/ChatAgent/utils/__tests__/fileLocation.test.ts',
  'src/pages/ChatAgent/utils/__tests__/filePaths.test.ts',
  'src/pages/ChatAgent/utils/__tests__/fileRefResolver.test.ts',
  'src/pages/ChatAgent/utils/__tests__/markdownBlocks.test.ts',
  'src/pages/ChatAgent/utils/__tests__/markdownSegments.test.ts',
  'src/pages/ChatAgent/utils/__tests__/normalizeFileRefs.test.ts',
  'src/pages/ChatAgent/utils/__tests__/parseErrorMessage.test.ts',
  'src/pages/ChatAgent/utils/__tests__/reasoningHeaders.test.ts',
  'src/pages/ChatAgent/utils/__tests__/scrollHelpers.test.ts',
  'src/pages/ChatAgent/utils/__tests__/structuredResult.test.ts',
  'src/pages/ChatAgent/utils/__tests__/threadRouteGuard.test.ts',
  'src/pages/ChatAgent/utils/__tests__/tokenUsage.test.ts',
  'src/pages/ChatAgent/utils/__tests__/turnFiles.test.ts',
  'src/pages/ChatAgent/utils/__tests__/uuid.test.ts',
  'src/pages/ChatAgent/utils/api/__tests__/computers.test.ts',
  'src/pages/ChatAgent/utils/api/__tests__/transport.errors.test.ts',
  'src/pages/Dashboard/utils/__tests__/api.test.ts',
  'src/pages/Dashboard/utils/__tests__/portfolioSummary.test.ts',
  'src/pages/Dashboard/widgets/definitions/__tests__/holdingsHelpers.test.ts',
  'src/pages/Dashboard/widgets/framework/__tests__/attribution-visibility.test.ts',
  'src/pages/Dashboard/widgets/framework/__tests__/snapshotSerializers.test.ts',
  'src/pages/Dashboard/widgets/framework/__tests__/tvEmbedColorScheme.test.ts',
  'src/pages/Login/__tests__/PasswordStrength.test.ts',
  'src/pages/Login/__tests__/passwordRequirements.test.ts',
  'src/pages/Login/__tests__/spxSeries.test.ts',
  'src/pages/MarketView/utils/__tests__/chartConstants.test.ts',
  'src/pages/MarketView/utils/__tests__/downsampleBars.test.ts',
  'src/pages/MarketView/utils/__tests__/marketRoute.test.ts',
  'src/pages/MarketView/utils/__tests__/selectionPrimitive.test.ts',
  'src/pages/MarketView/utils/__tests__/selectionSend.test.ts',
  'src/pages/MarketView/utils/__tests__/toolbarTiers.test.ts',
  'src/pages/Onboarding/__tests__/onboardingPrefsSchema.test.ts',
  'src/pages/Onboarding/__tests__/whatsNew.test.ts',
  'src/pages/Orders/__tests__/format.test.ts',
  'src/pages/Plugins/__tests__/bulkRun.test.ts',
  'src/pages/Plugins/__tests__/connectOutcome.test.ts',
  'src/pages/Plugins/__tests__/groupOrigins.test.ts',
  'src/pages/Plugins/__tests__/scopeTargets.test.ts',
  'src/pages/Plugins/__tests__/toolSelection.test.ts',
  'src/pages/Plugins/utils/__tests__/webLink.test.ts',
  'src/pages/Setup/steps/__tests__/ModelPickStepCustomModels.test.ts',
  'src/pages/Setup/steps/__tests__/mergeCustomModelsForSlug.test.ts',
  'src/pages/Setup/steps/__tests__/modelSlotCleanup.test.ts',
  'src/pages/SharedChat/__tests__/api.test.ts',
  'src/styles/__tests__/tokenRefs.test.ts',
  'src/types/__tests__/provenance.test.ts',
  'src/utils/__tests__/rateLimitError.test.ts',
];

// jsdom files a vm pool cannot run. There the global is a real jsdom Window:
// `window` and `location` cannot be redefined on it, and it lacks Node globals
// such as ReadableStream that forks keep beside jsdom.
const FORKS_FILES = [
  // Replace window.location to watch a full-page navigation.
  'src/pages/Login/__tests__/AuthConfirm.handoff.test.tsx',
  'src/pages/Plugins/__tests__/McpServers.test.tsx',
  'src/pages/Plugins/__tests__/brokerageSurface.test.tsx',
  // Replaces `window` itself to install the desktop bridge.
  'src/lib/__tests__/desktop.test.ts',
  // Need ReadableStream: two load jsdom themselves (its undici reads it at
  // import), the other builds an SSE body from one.
  'src/lib/__tests__/staleBuildPreBoot.test.ts',
  'src/lib/__tests__/localePreload.test.ts',
  'src/pages/ChatAgent/hooks/__tests__/useWarmWorkspaceSandbox.test.tsx',
];

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    // A vm worker keeps every file's module graph until its heap reaches this,
    // then restarts. The default is total memory / workers, which on a 16 GB,
    // 4-CPU CI runner lets three workers grow toward all 16 GB. At 1 GB a
    // 3-worker run peaks near 4 GB (7 GB uncapped) at the same wall time.
    // Read from the root config only.
    vmMemoryLimit: '1GB',
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          environment: 'node',
          include: NODE_FILES,
          setupFiles: ['./src/test/setup.node.ts'],
        },
      },
      {
        extends: true,
        test: {
          // A vm worker loads jsdom and compiles each module once, then gives
          // every file a fresh context. Forks pay both again per file.
          name: 'jsdom',
          environment: 'jsdom',
          pool: 'vmThreads',
          include: TEST_FILES,
          exclude: [...EXCLUDE, ...NODE_FILES, ...FORKS_FILES],
          setupFiles: ['./src/test/setup.ts'],
        },
      },
      {
        extends: true,
        test: {
          // Named to sort first: Vitest queues projects by name, and started
          // last these files ran up to 5x slower and set the run's tail.
          name: 'forks',
          environment: 'jsdom',
          pool: 'forks',
          include: FORKS_FILES,
          setupFiles: ['./src/test/setup.ts'],
        },
      },
    ],
  },
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
      // Fixtures a unit test shares with the Playwright specs.
      '@e2e': path.resolve(import.meta.dirname, './e2e'),
    },
  },
});
