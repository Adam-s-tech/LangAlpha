// @vitest-environment node
import { describe, it, expect } from 'vitest';
import {
  MEMORY_WORKSPACE_DIR,
  USER_DATA_FILES,
  classifyAgentPath,
  computeAgentArtifactRouting,
  isAgentNotesPath,
  isUserDataReadmePath,
  normalizeAgentHref,
  normalizeAgentPath,
  parseAgentHref,
  parseAgentPath,
  topicFromMemoryKey,
  workspaceRelativePath,
} from '../agentPaths';

/**
 * The one set of path rules. Every other helper is a projection of these, so
 * a path has to mean the same thing whichever route it arrived by.
 */
describe('normalizeAgentPath', () => {
  it('strips the sandbox root and folds dot segments', () => {
    expect(normalizeAgentPath('/home/workspace/results/report.md')).toBe('results/report.md');
    expect(normalizeAgentPath('file:///home/daytona/results/report.md')).toBe('results/report.md');
    expect(normalizeAgentPath('./results/../data/./x.csv')).toBe('data/x.csv');
  });

  it('keeps a true absolute path absolute', () => {
    expect(normalizeAgentPath('/tmp/out.csv')).toBe('/tmp/out.csv');
    expect(normalizeAgentPath('/large_tool_results/abc')).toBe('/large_tool_results/abc');
  });

  it('is idempotent, so a path may pass through more than one layer', () => {
    for (const raw of [
      '/home/workspace/a/../b.md', 'file:///home/daytona/x.csv', 'results/', '/tmp/y',
      // Names a decoded href arrives carrying. Each one used to lose everything
      // from its punctuation on, because the layer that read it a second time
      // read it as a URL again.
      'results/issue#1.md', 'results/a?b.md', '../data.csv', 'results/季度报告.md',
    ]) {
      expect(normalizeAgentPath(normalizeAgentPath(raw))).toBe(normalizeAgentPath(raw));
    }
  });

  it('keeps punctuation a file name is allowed to hold', () => {
    // A path, not a link: the `#` in `issue#1.md` is part of the name. Reading
    // it as a fragment truncated the path to `results/issue`, and the panel
    // then asked the server for a file nothing had written.
    expect(normalizeAgentPath('results/issue#1.md')).toBe('results/issue#1.md');
    expect(normalizeAgentPath('results/a?b.md')).toBe('results/a?b.md');
  });

  it('lets a relative path keep the levels it climbs', () => {
    // Dropping the `..` rewrote the reference into a different file, one that
    // often exists, so the link opened the wrong document instead of missing.
    expect(normalizeAgentPath('../data.csv')).toBe('../data.csv');
    expect(normalizeAgentPath('../../a/b.md')).toBe('../../a/b.md');
    expect(normalizeAgentPath('work/../../data.csv')).toBe('../data.csv');
    // A path that starts at a root has nowhere to climb.
    expect(normalizeAgentPath('/home/workspace/../etc/passwd')).toBe('etc/passwd');
    expect(normalizeAgentPath('/../etc/passwd')).toBe('/etc/passwd');
  });

  it('reports the workspace qualifier, the root and the directory intent', () => {
    expect(parseAgentPath('__wsref__/ws-7/results/review.md')).toEqual({
      workspaceId: 'ws-7', path: 'results/review.md', absolute: false, directory: false,
    });
    expect(parseAgentPath('/home/workspace/results/')).toMatchObject({ path: 'results/', absolute: true, directory: true });
    expect(parseAgentPath('./')).toMatchObject({ path: '', directory: true });
    expect(parseAgentPath('charts/fig.png')).toMatchObject({ absolute: false, directory: false });
  });
});

describe('normalizeAgentHref', () => {
  it('decodes percent escapes exactly once', () => {
    expect(normalizeAgentHref('results/a%20b.md')).toBe('results/a b.md');
    // The literal `%20` a correctly-encoded link spells `%2520`.
    expect(normalizeAgentHref('results/a%2520b.md')).toBe('results/a%20b.md');
  });

  it('drops a query or fragment a link can carry and a path cannot', () => {
    expect(normalizeAgentHref('results/report.md#heading')).toBe('results/report.md');
    expect(normalizeAgentHref('results/chart.png?v=2')).toBe('results/chart.png');
  });

  it('leaves a lone percent alone rather than throwing', () => {
    expect(normalizeAgentHref('results/100%_done.md')).toBe('results/100%_done.md');
  });
});

describe('workspaceRelativePath', () => {
  it('strips the /home/workspace sandbox root', () => {
    expect(workspaceRelativePath('/home/workspace/agent.md')).toBe('agent.md');
    expect(workspaceRelativePath('/home/workspace/work/scratch/chart.png')).toBe(
      'work/scratch/chart.png',
    );
  });

  it('strips the /home/daytona sandbox root', () => {
    expect(workspaceRelativePath('/home/daytona/notes.md')).toBe('notes.md');
  });

  it('collapses the bare sandbox root to empty (caller labels it)', () => {
    expect(workspaceRelativePath('/home/workspace')).toBe('');
    expect(workspaceRelativePath('/home/daytona')).toBe('');
  });

  it('unwraps file:/// and ./ forms like the router', () => {
    expect(workspaceRelativePath('file:///home/workspace/a.md')).toBe('a.md');
    expect(workspaceRelativePath('./work/out.csv')).toBe('work/out.csv');
  });

  it('leaves a path with no sandbox prefix unchanged', () => {
    expect(workspaceRelativePath('.agents/user/memo/note.md')).toBe(
      '.agents/user/memo/note.md',
    );
  });
});

/**
 * A destination is a URL and a path is not, and the two readings differ by one
 * rule. The reading has to be chosen once, at the layer that holds the string,
 * because `normalizeAgentHref` decodes: after it runs, a `%23` has become a
 * literal `#` and any second reading as a URL eats the rest of the name.
 */
describe('normalizeAgentHref — a destination, read once', () => {
  it('decodes a percent-escaped name and hands back a path', () => {
    expect(normalizeAgentHref('results/%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.md')).toBe('results/季度报告.md');
    expect(normalizeAgentHref('results/Q3%20deck.pptx')).toBe('results/Q3 deck.pptx');
    expect(normalizeAgentHref('results/report%20(final).md')).toBe('results/report (final).md');
  });

  it('gives an escaped `#` or `?` back as part of the name', () => {
    expect(normalizeAgentHref('results/issue%231.md')).toBe('results/issue#1.md');
    expect(normalizeAgentHref('results/a%3Fb.md')).toBe('results/a?b.md');
  });

  it('still reads an unescaped `?` or `#` as link syntax', () => {
    expect(normalizeAgentHref('results/report.md?ts=1')).toBe('results/report.md');
    expect(normalizeAgentHref('.agents/user/memo/foo.md#sec')).toBe('.agents/user/memo/foo.md');
  });

  it('leaves a lone `%` alone rather than reading it as a broken escape', () => {
    expect(normalizeAgentHref('results/100% done.md')).toBe('results/100% done.md');
    expect(normalizeAgentHref('results/100%25 done.md')).toBe('results/100% done.md');
  });

  it('carries non-Latin names and the punctuation around them intact', () => {
    for (const name of [
      'results/季度报告.md', 'results/日本語 ファイル.pdf', 'results/한국어 보고서.md',
      'results/Отчёт 2026.md', 'results/naïve résumé.docx', 'results/图表 📊.png',
      'results/tag[1].md', 'results/a&b.md', 'results/a+b.md', '分析/结果.md',
    ]) {
      expect(normalizeAgentHref(name)).toBe(name);
    }
  });
});

describe('classifyAgentPath', () => {
  it('classifies a user memory entry', () => {
    const r = classifyAgentPath('.agents/user/memory/risk-preferences.md');
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') {
      expect(r.tier).toBe('user');
      expect(r.key).toBe('risk-preferences.md');
      expect(r.isIndex).toBe(false);
    }
  });

  it('flags the user memory index', () => {
    const r = classifyAgentPath('.agents/user/memory/memory.md');
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') {
      expect(r.tier).toBe('user');
      expect(r.isIndex).toBe(true);
    }
  });

  it('classifies a workspace memory entry', () => {
    // Built from the generated dir: the spelling belongs to paths.py, and the
    // contract under test is that a path under it classifies as memory.
    const r = classifyAgentPath(`${MEMORY_WORKSPACE_DIR}/foo.md`);
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') expect(r.tier).toBe('workspace');
  });

  // The pre-folder spelling, written out on purpose: the generated constant no
  // longer carries it, and stored transcripts still do.
  it('classifies a legacy workspace memory entry, bare and root-prefixed', () => {
    for (const p of [
      '.agents/workspace/memory/foo.md',
      '/home/workspace/.agents/workspace/memory/foo.md',
      'file:///home/daytona/.agents/workspace/memory/foo.md',
    ]) {
      expect(classifyAgentPath(p)).toMatchObject({ kind: 'memory', tier: 'workspace', key: 'foo.md' });
    }
    expect(classifyAgentPath('.agents/workspace/memory/memory.md')).toMatchObject({
      kind: 'memory', tier: 'workspace', isIndex: true,
    });
  });

  it('classifies a memo entry, slug opaque', () => {
    const r = classifyAgentPath('.agents/user/memo/my-report.pdf');
    expect(r.kind).toBe('memo');
    if (r.kind === 'memo') {
      expect(r.key).toBe('my-report.pdf');
      expect(r.isIndex).toBe(false);
    }
  });

  it('flags the memo index', () => {
    const r = classifyAgentPath('.agents/user/memo/memo.md');
    expect(r.kind).toBe('memo');
    if (r.kind === 'memo') expect(r.isIndex).toBe(true);
  });

  it('classifies a skill activation', () => {
    const r = classifyAgentPath('.agents/skills/investigate/SKILL.md');
    expect(r.kind).toBe('skill');
    if (r.kind === 'skill') expect(r.name).toBe('investigate');
  });

  it('strips the leading slash', () => {
    const r = classifyAgentPath('/.agents/user/memory/foo.md');
    expect(r.kind).toBe('memory');
  });

  it('strips the home/workspace/ sandbox-root prefix', () => {
    const r = classifyAgentPath('home/workspace/.agents/user/memory/foo.md');
    expect(r.kind).toBe('memory');
  });

  it('treats every well-formed shape identically', () => {
    const variants = [
      '.agents/user/memory/memory.md',
      '/.agents/user/memory/memory.md',
      'home/workspace/.agents/user/memory/memory.md',
    ];
    const kinds = variants.map((p) => classifyAgentPath(p).kind);
    expect(new Set(kinds)).toEqual(new Set(['memory']));
  });

  it('falls back to file for unknown paths', () => {
    expect(classifyAgentPath('work/notes.md').kind).toBe('file');
    expect(classifyAgentPath('').kind).toBe('file');
  });

  it('treats bare memory.md / memo.md (no prefix) as a regular file', () => {
    // Agent middleware always emits the full prefix; bare names are user files.
    expect(classifyAgentPath('memory.md').kind).toBe('file');
    expect(classifyAgentPath('memo.md').kind).toBe('file');
  });

  it('unwraps __wsref__/<wsid>/... and decorates with crossWorkspaceId', () => {
    const r = classifyAgentPath('__wsref__/abc-123/.agents/user/memory/risk.md');
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') {
      expect(r.tier).toBe('user');
      expect(r.key).toBe('risk.md');
      expect(r.crossWorkspaceId).toBe('abc-123');
    }
  });

  it('unwraps __wsref__ for workspace memory and propagates the wsid', () => {
    const r = classifyAgentPath(`__wsref__/ws-X/${MEMORY_WORKSPACE_DIR}/notes.md`);
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') {
      expect(r.tier).toBe('workspace');
      expect(r.key).toBe('notes.md');
      expect(r.crossWorkspaceId).toBe('ws-X');
    }
  });

  it('unwraps __wsref__ for legacy workspace memory and propagates the wsid', () => {
    const r = classifyAgentPath('__wsref__/ws-X/.agents/workspace/memory/notes.md');
    expect(r.kind).toBe('memory');
    if (r.kind === 'memory') {
      expect(r.tier).toBe('workspace');
      expect(r.key).toBe('notes.md');
      expect(r.crossWorkspaceId).toBe('ws-X');
    }
  });

  it('strips the file:///home/workspace/ markdown auto-link prefix', () => {
    const r = classifyAgentPath('file:///home/workspace/.agents/user/memo/x.md');
    expect(r.kind).toBe('memo');
    if (r.kind === 'memo') {
      expect(r.key).toBe('x.md');
    }
  });

  it('strips the bare /home/daytona/ sandbox-absolute prefix', () => {
    const r = classifyAgentPath('/home/daytona/.agents/skills/foo/SKILL.md');
    expect(r.kind).toBe('skill');
    if (r.kind === 'skill') {
      expect(r.name).toBe('foo');
    }
  });

  it('strips a leading ./ before classification', () => {
    const r = classifyAgentPath('./.agents/user/memory/foo.md');
    expect(r.kind).toBe('memory');
  });

  it('strips trailing ?query and #fragment before classification', () => {
    const r = classifyAgentPath('.agents/user/memo/foo.md?ts=1#sec');
    expect(r.kind).toBe('memo');
    if (r.kind === 'memo') {
      expect(r.key).toBe('foo.md');
    }
  });

  it('falls back to file for malformed memory dir paths (trailing slash)', () => {
    // `.agents/user/memory/` has empty key — would trigger MemoryPanel's
    // not-found banner. Treat as a Files-tab dir reference instead.
    expect(classifyAgentPath('.agents/user/memory/').kind).toBe('file');
    expect(classifyAgentPath(`${MEMORY_WORKSPACE_DIR}/`).kind).toBe('file');
    expect(classifyAgentPath('.agents/workspace/memory/').kind).toBe('file');
  });

  describe('user-data classification', () => {
    it('classifies every file in USER_DATA_FILES by its name without .json', () => {
      for (const [dir, files] of Object.entries(USER_DATA_FILES)) {
        for (const file of files) {
          const r = classifyAgentPath(`${dir}/${file}`);
          expect(r.kind).toBe('user-data');
          if (r.kind === 'user-data') expect(r.entity).toBe(file.replace(/\.json$/, ''));
        }
      }
    });

    it('classifies the profile files and each automation file', () => {
      const entity = (path: string) => {
        const r = classifyAgentPath(path);
        return r.kind === 'user-data' ? r.entity : null;
      };
      expect(entity('.agents/user/profile/portfolio.json')).toBe('portfolio');
      expect(entity('.agents/user/profile/watchlist.json')).toBe('watchlist');
      expect(entity('.agents/user/profile/preference.json')).toBe('preference');
      // One file per automation, under any name the server accepts.
      expect(entity('.agents/user/automations/morning-brief.json')).toBe('automations');
      expect(entity('.agents/user/automations/AAPL_below_200.json')).toBe('automations');
      expect(entity('.agents/user/automations/portfolio.json')).toBe('automations');
    });

    it('strips the sandbox-root prefix', () => {
      expect(classifyAgentPath('home/workspace/.agents/user/profile/portfolio.json').kind).toBe('user-data');
      expect(classifyAgentPath('/home/workspace/.agents/user/automations/morning-brief.json').kind).toBe('user-data');
    });

    it('falls back to file for names no data file takes and the directories themselves', () => {
      expect(classifyAgentPath('.agents/user/profile/other.json').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/profile/').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/automations/').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/automations/notes.txt').kind).toBe('file');
      // An editor's temporary file, which the server refuses too.
      expect(classifyAgentPath('.agents/user/automations/brief.json.tmp').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/automations/-brief.json').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/automations/sub/brief.json').kind).toBe('file');
      expect(classifyAgentPath(`.agents/user/automations/${'a'.repeat(65)}.json`).kind).toBe('file');
      expect(classifyAgentPath('.agents/user/profile/morning-brief.json').kind).toBe('file');
    });

    it('classifies the README beside the data files as a generic file', () => {
      // README is classified generically; hiding happens via
      // `isUserDataReadmePath` at the UI layer, not via the routing kind.
      expect(classifyAgentPath('.agents/user/profile/README.md').kind).toBe('file');
      expect(classifyAgentPath('.agents/user/automations/README.md').kind).toBe('file');
    });

    it('unwraps __wsref__ and propagates crossWorkspaceId', () => {
      const profile = classifyAgentPath('__wsref__/ws-7/.agents/user/profile/portfolio.json');
      expect(profile.kind).toBe('user-data');
      if (profile.kind === 'user-data') {
        expect(profile.entity).toBe('portfolio');
        expect(profile.crossWorkspaceId).toBe('ws-7');
      }
      const automations = classifyAgentPath('__wsref__/ws-7/.agents/user/automations/morning-brief.json');
      expect(automations.kind).toBe('user-data');
      if (automations.kind === 'user-data') {
        expect(automations.entity).toBe('automations');
        expect(automations.crossWorkspaceId).toBe('ws-7');
      }
    });
  });
});

describe('computeAgentArtifactRouting: user data', () => {
  it('routes portfolio.json to the Files tab and clears the workspace id', () => {
    const r = computeAgentArtifactRouting('.agents/user/profile/portfolio.json');
    expect(r.targetFile).toBe('.agents/user/profile/portfolio.json');
    expect(r.clearWorkspaceId).toBe(true);
    // Mutually exclusive with memory/memo targets
    expect(r.targetMemoryKey).toBeNull();
    expect(r.targetMemoKey).toBeNull();
  });

  it('does not set setWorkspaceId for user-scoped profile paths', () => {
    const r = computeAgentArtifactRouting('.agents/user/profile/watchlist.json', 'ws-A');
    // The profile is global to the user; ignore caller-supplied wsid.
    expect(r.setWorkspaceId).toBeNull();
    expect(r.clearWorkspaceId).toBe(true);
  });

  it('routes an automation file to the Files tab as user-scoped, ignoring a caller wsid', () => {
    const r = computeAgentArtifactRouting('.agents/user/automations/morning-brief.json', 'ws-A');
    expect(r.targetFile).toBe('.agents/user/automations/morning-brief.json');
    expect(r.setWorkspaceId).toBeNull();
    expect(r.clearWorkspaceId).toBe(true);
  });

  it('ignores an embedded __wsref__ id too', () => {
    const r = computeAgentArtifactRouting('__wsref__/ws-7/.agents/user/profile/portfolio.json');
    expect(r.setWorkspaceId).toBeNull();
    expect(r.clearWorkspaceId).toBe(true);
  });
});

describe('topicFromMemoryKey', () => {
  it('strips .md and replaces dashes/underscores', () => {
    expect(topicFromMemoryKey('risk-preferences.md')).toBe('risk preferences');
    expect(topicFromMemoryKey('my_topic.md')).toBe('my topic');
    expect(topicFromMemoryKey('plain.md')).toBe('plain');
  });

  it('handles edge cases', () => {
    expect(topicFromMemoryKey('')).toBe('');
    expect(topicFromMemoryKey('mixed-with_both.md')).toBe('mixed with both');
    expect(topicFromMemoryKey('NoExt')).toBe('NoExt');
  });
});

describe('isUserDataReadmePath', () => {
  it('matches the relative path', () => {
    expect(isUserDataReadmePath('.agents/user/profile/README.md')).toBe(true);
    expect(isUserDataReadmePath('.agents/user/automations/README.md')).toBe(true);
  });

  it('matches an absolute sandbox path', () => {
    expect(isUserDataReadmePath('/home/workspace/.agents/user/profile/README.md')).toBe(true);
    expect(isUserDataReadmePath('home/daytona/.agents/user/profile/README.md')).toBe(true);
    expect(isUserDataReadmePath('/home/workspace/.agents/user/automations/README.md')).toBe(true);
  });

  it('matches a file:/// wrapped path', () => {
    expect(
      isUserDataReadmePath('file:///home/workspace/.agents/user/profile/README.md'),
    ).toBe(true);
  });

  it('matches a __wsref__ cross-workspace path', () => {
    expect(
      isUserDataReadmePath('__wsref__/ws-7/.agents/user/profile/README.md'),
    ).toBe(true);
  });

  it('does not match the data files', () => {
    expect(isUserDataReadmePath('.agents/user/profile/portfolio.json')).toBe(false);
    expect(isUserDataReadmePath('.agents/user/profile/watchlist.json')).toBe(false);
    expect(isUserDataReadmePath('.agents/user/profile/preference.json')).toBe(false);
    expect(isUserDataReadmePath('.agents/user/automations/morning-brief.json')).toBe(false);
  });

  it('does not match other READMEs in the sandbox', () => {
    expect(isUserDataReadmePath('README.md')).toBe(false);
    expect(isUserDataReadmePath('.agents/skills/some-skill/README.md')).toBe(false);
    expect(isUserDataReadmePath('home/workspace/work/scratch/README.md')).toBe(false);
  });

  it('handles empty / nonsense input safely', () => {
    expect(isUserDataReadmePath('')).toBe(false);
    expect(isUserDataReadmePath('not-a-path')).toBe(false);
  });
});

describe('isAgentNotesPath', () => {
  const notes = (raw: string, dir?: string | null) => isAgentNotesPath(parseAgentPath(raw), dir);

  it('matches the notes file at the workspace root', () => {
    expect(notes('agent.md')).toBe(true);
    expect(notes('./agent.md')).toBe(true);
    expect(notes('/home/workspace/agent.md')).toBe(true);
    expect(notes('file:///home/daytona/agent.md')).toBe(true);
  });

  it('matches the notes file under the workspace\'s project folder', () => {
    expect(notes('/home/workspace/alpha/agent.md', 'alpha')).toBe(true);
    // Relative paths already resolve inside the folder, so this one is nested.
    expect(notes('alpha/agent.md', 'alpha')).toBe(false);
  });

  it('keeps an agent.md in any other folder', () => {
    expect(notes('/home/workspace/docs/agent.md', 'alpha')).toBe(false);
    expect(notes('/home/workspace/docs/agent.md')).toBe(false);
    expect(notes('reports/agent.md')).toBe(false);
    expect(notes('reports/agent.md', 'alpha')).toBe(false);
  });

  it('keeps an agent.md rooted outside the sandbox', () => {
    expect(notes('/tmp/agent.md')).toBe(false);
    expect(notes('/tmp/agent.md', 'tmp')).toBe(false);
    expect(notes('/alpha/agent.md', 'alpha')).toBe(false);
  });

  it('reads a link the same way as a tool path', () => {
    expect(isAgentNotesPath(parseAgentHref('agent.md#notes'))).toBe(true);
    expect(isAgentNotesPath(parseAgentHref('/home/workspace/alpha/agent.md?v=2'), 'alpha')).toBe(true);
    expect(isAgentNotesPath(parseAgentHref('agent.md/'))).toBe(false);
  });

  it('matches the notes file in a folder a rename moved the workspace out of', () => {
    const renamed = (raw: string) => isAgentNotesPath(parseAgentPath(raw), 'Research', ['research-ab12', 'alpha']);
    expect(renamed('/home/workspace/Research/agent.md')).toBe(true);
    expect(renamed('/home/workspace/research-ab12/agent.md')).toBe(true);
    expect(renamed('file:///home/daytona/alpha/agent.md')).toBe(true);
    // Still only a sandbox-anchored path names the folder.
    expect(renamed('research-ab12/agent.md')).toBe(false);
    expect(renamed('/home/workspace/beta/agent.md')).toBe(false);
    // Without a current folder the former ones still count.
    expect(isAgentNotesPath(parseAgentPath('/home/workspace/alpha/agent.md'), null, ['alpha'])).toBe(true);
  });
});

describe('computeAgentArtifactRouting: a renamed workspace', () => {
  const route = (raw: string) => computeAgentArtifactRouting(raw, undefined, 'Research', ['research-ab12', 'older']);

  it('folds a path an older turn wrote under a former folder', () => {
    expect(route('/home/workspace/research-ab12/report.md')).toMatchObject({ targetFile: 'report.md' });
    expect(route('file:///home/workspace/older/results/q3.csv')).toMatchObject({ targetFile: 'results/q3.csv' });
    expect(route('/home/workspace/research-ab12/results/')).toMatchObject({ targetFile: null, targetDirectory: 'results' });
    expect(route('/home/workspace/research-ab12/')).toMatchObject({ targetFile: null, targetDirectory: '' });
  });

  it('still folds the current folder', () => {
    expect(route('/home/workspace/Research/report.md')).toMatchObject({ targetFile: 'report.md' });
  });

  it('routes a former folder\'s store paths to their own tabs', () => {
    expect(route(`/home/workspace/research-ab12/${MEMORY_WORKSPACE_DIR}/risk.md`)).toMatchObject({
      targetMemoryKey: 'risk.md',
      targetMemoryTier: 'workspace',
    });
  });

  it('leaves relative paths and other folders alone', () => {
    expect(route('research-ab12/report.md')).toMatchObject({ targetFile: 'research-ab12/report.md' });
    expect(route('/home/workspace/other/report.md')).toMatchObject({ targetFile: '/home/workspace/other/report.md' });
  });

  it('folds one folder, not a subfolder that shares a former name', () => {
    expect(route('/home/workspace/Research/research-ab12/x.md')).toMatchObject({ targetFile: 'research-ab12/x.md' });
  });

  it('matches a former folder by its name key and the current one exactly, as the server does', () => {
    expect(route('/home/workspace/RESEARCH-AB12/report.md')).toMatchObject({ targetFile: 'report.md' });
    expect(route('/home/workspace/RESEARCH/report.md')).toMatchObject({ targetFile: '/home/workspace/RESEARCH/report.md' });
    const renamed = (raw: string) => computeAgentArtifactRouting(raw, undefined, 'Weg', ['Straße']);
    expect(renamed('/home/workspace/STRASSE/plan.md')).toMatchObject({ targetFile: 'plan.md' });
  });

  it('keeps a folder casefold tells apart from the former one', () => {
    const dotless = (raw: string) => computeAgentArtifactRouting(raw, undefined, 'Now', ['\u0131']);
    expect(dotless('/home/workspace/\u0131/report.md')).toMatchObject({ targetFile: 'report.md' });
    expect(dotless('/home/workspace/i/report.md')).toMatchObject({ targetFile: '/home/workspace/i/report.md' });
    expect(dotless('/home/workspace/I/report.md')).toMatchObject({ targetFile: '/home/workspace/I/report.md' });
    const cherokee = (raw: string) => computeAgentArtifactRouting(raw, undefined, 'Now', ['\u13a0']);
    expect(cherokee('/home/workspace/\uab70/report.md')).toMatchObject({ targetFile: 'report.md' });
  });
});
