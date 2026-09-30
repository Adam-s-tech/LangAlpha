/**
 * The amber dot clears when the tab re-reads its file, even where nothing
 * else about the strip changed. A compiled strip caches each tab on the
 * `hasChanged` it was handed, so a read mark has to reach it as a new
 * function, not as a counter bump behind the same one.
 */
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { TabStrip } from '../TabStrip';
import { useChangedFiles } from '../useChangedFiles';
import type { FileTab } from '../useFileTabs';
import type { WriteEvent } from '../../../utils/fileRefResolver';

const tabs: FileTab[] = [{ id: 'a', kind: 'file', path: 'notes.md', preview: false, location: null, locationSeq: 0 }];
const noop = () => {};

function Strip({ getWriteLog }: { getWriteLog: () => WriteEvent[] }) {
  const changed = useChangedFiles(getWriteLog);
  return (
    <>
      <TabStrip
        tabs={tabs}
        activeId="a"
        onActivate={noop}
        onClose={noop}
        onPin={noop}
        onNewTab={null}
        hasChanged={changed.hasChanged}
        treeOpen={false}
        onToggleTree={null}
        onPanelClose={null}
      />
      <button type="button" onClick={() => changed.markRead('notes.md')}>read</button>
    </>
  );
}

describe('TabStrip changed dot', () => {
  it('sets on a write after the read and clears on the next read', () => {
    const log = { current: [] as WriteEvent[] };
    const getWriteLog = () => log.current;
    const { rerender } = render(<Strip getWriteLog={getWriteLog} />);
    fireEvent.click(screen.getByText('read'));
    expect(screen.queryByTitle('Changed since you opened it')).toBeNull();

    log.current = [{ id: 'w1', path: 'notes.md' }];
    rerender(<Strip getWriteLog={getWriteLog} />);
    expect(screen.getByTitle('Changed since you opened it')).toBeInTheDocument();

    fireEvent.click(screen.getByText('read'));
    expect(screen.queryByTitle('Changed since you opened it')).toBeNull();
  });
});
