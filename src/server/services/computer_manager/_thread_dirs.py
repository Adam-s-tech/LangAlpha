"""The per-thread dirs a workspace keeps on the sandbox, and the prune of a
deleted thread's that the bring-up runs."""

import logging
import shlex
from dataclasses import dataclass
from typing import Any

from ptc_agent.core.paths import THREAD_DIR_NAME, WorkspaceLayout

logger = logging.getLogger(__name__)

_LIST_SCRIPT = """
for d in {threads}/*/; do
  [ -d "$d" ] && echo "d $(basename "$d")"
done
for d in {results}/*/; do
  [ -d "$d" ] && echo "r $(basename "$d")"
done
true
"""


@dataclass
class ThreadDirListing:
    thread_dirs: set[str]
    result_dirs: set[str]


def listing_script(layout: WorkspaceLayout) -> str:
    """The shell that lists what ``prune_dead_thread_dirs`` judges, for a
    caller to run inside a script of its own and hand back parsed."""
    return _LIST_SCRIPT.format(
        threads=shlex.quote(layout.join(WorkspaceLayout.THREADS_DIR)),
        results=shlex.quote(layout.join(WorkspaceLayout.LARGE_TOOL_RESULTS_DIR)),
    )


def parse_listing(stdout: str) -> ThreadDirListing:
    found = ThreadDirListing(set(), set())
    for line in stdout.splitlines():
        parts = line.split(" ")
        if parts[0] == "d" and len(parts) == 2:
            found.thread_dirs.add(parts[1])
        elif parts[0] == "r" and len(parts) == 2:
            found.result_dirs.add(parts[1])
    return found


async def prune_dead_thread_dirs(
    runtime: Any,
    layout: WorkspaceLayout,
    workspace_id: str,
    *,
    listing: ThreadDirListing | None = None,
) -> tuple[set[str], bool]:
    """Remove the dirs of deleted threads; return the live threads' short ids
    and whether every dead dir is gone, which a bring-up stamp may claim.

    A delete while the machine was down leaves its dirs behind. The listing
    runs before the thread read, so a thread created meanwhile is live by the
    time its dir could be judged; a caller passing ``listing`` ran
    ``listing_script`` before this call. The caller holds the folder
    throughout.
    """
    from src.server.database.conversation import get_workspace_thread_short_ids

    if listing is None:
        listed = await runtime.exec(listing_script(layout))
        # The script ends in ``true``: only a failed exec exits otherwise, and
        # its empty output would read as no dirs at all.
        listing = parse_listing(listed.stdout or "") if listed.exit_code == 0 else None
    live_short = await get_workspace_thread_short_ids(workspace_id)
    if listing is None:
        return live_short, False
    threads_dir = layout.join(WorkspaceLayout.THREADS_DIR)
    results_dir = layout.join(WorkspaceLayout.LARGE_TOOL_RESULTS_DIR)
    dead = [
        f"{base}/{name}"
        for base, names in (
            (threads_dir, listing.thread_dirs),
            (results_dir, listing.result_dirs),
        )
        for name in names
        if THREAD_DIR_NAME.match(name) and name not in live_short
    ]
    if not dead:
        return live_short, True
    removed = await runtime.exec("rm -rf -- " + " ".join(shlex.quote(p) for p in dead))
    if removed.exit_code != 0:
        logger.warning(
            f"Workspace {workspace_id}: could not remove {len(dead)} dead thread "
            f"dirs (exit {removed.exit_code}): {(removed.stdout or removed.stderr or '')[:200]}"
        )
        return live_short, False
    logger.info(f"Workspace {workspace_id}: removed {len(dead)} dead thread dirs")
    return live_short, True
