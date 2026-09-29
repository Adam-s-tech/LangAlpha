"""Each thread's transcript, kept in its workspace folder as JSONL the agent can grep.

The checkpoint is the record and these files are a cache of it: rebuilt at turn
end and whenever a workspace comes up on a machine, and never backed up. Every
thread manifest carries a fingerprint of the checkpoint and task runs it was
rendered from, so a sync that finds a thread current reads no state at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from ptc_agent.agent.transcript import (
    MANIFEST,
    TASK_META,
    TRANSCRIPT_DIR,
    load_manifest,
    plan_writes,
    render,
    task_subdir,
    transcript_subdir,
)
from ptc_agent.agent.transcript.render import SCHEMA_VERSION
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout

logger = logging.getLogger(__name__)

INDEX = "index.jsonl"

_SHORT_ID = re.compile(r"^[0-9a-f]{8}$")
# Backfill of a workspace with many old threads: each export holds one
# checkpointer connection while it reads, and task reads fan out below it.
_EXPORT_CONCURRENCY = 3
_TASK_READ_CONCURRENCY = 4


@asynccontextmanager
async def held_layout(root: str, workspace_id: str) -> AsyncIterator[WorkspaceLayout | None]:
    """The workspace's folder, read afresh and kept in place until exit.

    A settle renames a folder whenever no hold covers it, so a folder read
    earlier (at turn start, when a job was queued) may name one a sibling now
    holds. None while the folder is staged mid-move or the workspace has none;
    the next turn end or bring-up catches up.
    """
    from src.server.database.workspace import get_workspace_dir_name
    from src.server.database.workspace_folders import (
        is_top_level,
        workspace_folder_in_use,
    )

    async with workspace_folder_in_use(workspace_id):
        dir_name = await get_workspace_dir_name(workspace_id)
        yield (
            SandboxLayout.for_root(root).for_workspace(dir_name)
            if dir_name and is_top_level(dir_name)
            else None
        )


def transcript_dir(layout: WorkspaceLayout, thread_id: str) -> str:
    return layout.join(transcript_subdir(thread_id[:8]))


def index_path(root: str) -> str:
    """The computer's thread index, beside every workspace folder rather than in one."""
    return f"{root.rstrip('/')}/{WorkspaceLayout.THREADS_DIR}/{INDEX}"


def _task_print(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in ("latest_run_id", "status", "final_checkpoint_id")
    )


def _fingerprint(checkpoint_id: str | None, prints: dict[str, str]) -> str:
    payload = json.dumps([SCHEMA_VERSION, checkpoint_id, sorted(prints.items())])
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


async def _read_manifest(runtime: Any, path: str) -> dict[str, Any]:
    try:
        data = await runtime.download_file(path)
    except Exception:
        return {}
    return load_manifest(data.decode(errors="replace") if data else None)


async def _apply(runtime: Any, writes: dict[str, str], removals: list[str]) -> None:
    if writes:
        await runtime.upload_files(
            [(content.encode(), path) for path, content in writes.items()]
        )
    if removals:
        await runtime.exec("rm -rf " + " ".join(shlex.quote(p) for p in removals))


async def plan_thread(
    runtime: Any,
    layout: WorkspaceLayout,
    thread_id: str,
    *,
    checkpoint_id: str | None,
    tasks: list[dict[str, Any]],
    previous: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """What to write and remove so a thread's transcript matches its checkpoint.

    Only tasks whose latest run changed are read and rewritten; an unchanged
    thread costs nothing past the fingerprint comparison.
    """
    from src.server.services.history.reader import CheckpointHistoryReader

    prints = {row["task_id"]: _task_print(row) for row in tasks}
    fingerprint = _fingerprint(checkpoint_id, prints)
    if previous.get("fingerprint") == fingerprint:
        return {}, []

    reader = CheckpointHistoryReader.get_instance()
    state = await reader.aget_state(thread_id, checkpoint_id)
    rendered_at = (state.config or {}).get("configurable", {}).get("checkpoint_id")
    older = previous.get("checkpoint_id")
    # Checkpoint ids sort by time, so an export racing a newer one (another
    # worker's turn end) stands down instead of removing the newer turn.
    if older and rendered_at and older > rendered_at:
        return {}, []
    messages = list((state.values or {}).get("messages") or [])
    if not messages and not previous:
        return {}, []

    known = previous.get("tasks") if isinstance(previous.get("tasks"), dict) else {}
    changed = [row for row in tasks if known.get(row["task_id"]) != prints[row["task_id"]]]
    gate = asyncio.Semaphore(_TASK_READ_CONCURRENCY)

    async def read_task(task_id: str) -> list[Any]:
        async with gate:
            return (await reader.aget_task_history(thread_id, task_id)).messages

    task_messages = await asyncio.gather(*(read_task(row["task_id"]) for row in changed))

    directory = transcript_dir(layout, thread_id)
    writes, removals = plan_writes(
        directory,
        render(messages),
        previous=previous,
        header={
            "thread_id": thread_id,
            "checkpoint_id": rendered_at,
            "fingerprint": fingerprint,
            "tasks": prints,
        },
    )
    for row, run_messages in zip(changed, task_messages):
        task_writes, _ = plan_writes(
            layout.join(task_subdir(thread_id[:8], row["task_id"])),
            render(run_messages, unit="run"),
            previous={},
            unit="run",
            manifest_name=TASK_META,
            header={
                "task_id": row["task_id"],
                "description": row.get("description"),
                "subagent_type": row.get("subagent_type"),
                "status": row.get("status"),
                "created_at": _iso(row.get("created_at")),
                "launch_call_id": row.get("launch_tool_call_id"),
            },
        )
        writes.update(task_writes)
    removals += [
        layout.join(task_subdir(thread_id[:8], task_id))
        for task_id in known.keys() - prints.keys()
    ]
    return writes, removals


def _index_content(root: str, rows: list[dict[str, Any]]) -> str:
    machine = SandboxLayout.for_root(root)
    lines = []
    for row in rows:
        # A folder staged mid-move is under _internal until it lands.
        if not row.get("dir_name") or "/" in row["dir_name"]:
            continue
        thread_id = str(row["conversation_thread_id"])
        entry = {
            "thread_id": thread_id,
            "title": row.get("title"),
            "workspace": row.get("workspace_name"),
            "workspace_id": str(row["workspace_id"]),
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
            "transcript": transcript_dir(machine.for_workspace(row["dir_name"]), thread_id),
        }
        lines.append(json.dumps(entry, ensure_ascii=False, default=str))
    return "".join(line + "\n" for line in lines)


async def _computer_id(workspace_id: str) -> str | None:
    from src.server.database.computer import get_computer_for_workspace

    computer = await get_computer_for_workspace(workspace_id)
    return str(computer["computer_id"]) if computer else None


async def export_thread(
    runtime: Any, root: str, workspace_id: str, thread_id: str
) -> int:
    """Bring one thread's transcript and the computer index up to date."""
    async with held_layout(root, workspace_id) as layout:
        if layout is None:
            return 0
        return await _export_thread(runtime, layout, workspace_id, thread_id)


async def _export_thread(
    runtime: Any, layout: WorkspaceLayout, workspace_id: str, thread_id: str
) -> int:
    from src.server.database.conversation import (
        get_thread_checkpoint_id,
        list_computer_threads,
    )
    from src.server.database.runs.subagent_runs import list_thread_tasks

    checkpoint_id, tasks, computer_id = await asyncio.gather(
        get_thread_checkpoint_id(thread_id),
        list_thread_tasks([thread_id]),
        _computer_id(workspace_id),
    )
    previous = await _read_manifest(
        runtime, f"{transcript_dir(layout, thread_id)}/{MANIFEST}"
    )
    writes, removals = await plan_thread(
        runtime,
        layout,
        thread_id,
        checkpoint_id=checkpoint_id,
        tasks=tasks,
        previous=previous,
    )
    if computer_id:
        writes[index_path(layout.root)] = _index_content(
            layout.root, await list_computer_threads(computer_id)
        )
    await _apply(runtime, writes, removals)
    return len(writes)


_LIST_SCRIPT = """
for d in {threads}/*/; do
  [ -d "$d" ] || continue
  fp=$(grep -m1 -o '"fingerprint": "[^"]*"' "${{d}}{transcript}/{manifest}" 2>/dev/null | cut -d'"' -f4)
  echo "t $(basename "$d") $fp"
done
for d in {results}/*/; do
  [ -d "$d" ] && echo "r $(basename "$d")"
done
true
"""


async def sync_workspace(
    runtime: Any, layout: WorkspaceLayout, workspace_id: str
) -> dict[str, int]:
    """Reconcile a workspace's thread dirs with its threads, then export stale ones.

    Dirs of deleted threads go (a delete while the machine was down leaves
    them), threads whose manifest fingerprint is behind are re-exported newest
    first, and the computer index is rewritten. The one listing runs before
    the thread read, so a thread created meanwhile is live by the time its dir
    could be judged.
    """
    from src.server.database.conversation import (
        get_workspace_thread_short_ids,
        list_computer_threads,
    )
    from src.server.database.runs.subagent_runs import list_thread_tasks

    threads_dir = layout.join(WorkspaceLayout.THREADS_DIR)
    results_dir = layout.join(WorkspaceLayout.LARGE_TOOL_RESULTS_DIR)
    listing = await runtime.exec(
        _LIST_SCRIPT.format(
            threads=shlex.quote(threads_dir),
            results=shlex.quote(results_dir),
            transcript=TRANSCRIPT_DIR,
            manifest=MANIFEST,
        )
    )
    exported: dict[str, str] = {}
    result_dirs: set[str] = set()
    for line in (listing.stdout or "").splitlines():
        parts = line.split(" ")
        if parts[0] == "t" and len(parts) >= 2:
            exported[parts[1]] = parts[2] if len(parts) > 2 else ""
        elif parts[0] == "r" and len(parts) == 2:
            result_dirs.add(parts[1])

    live_short = await get_workspace_thread_short_ids(workspace_id)
    dead = [
        f"{base}/{name}"
        for base, names in ((threads_dir, exported), (results_dir, result_dirs))
        for name in names
        if _SHORT_ID.match(name) and name not in live_short
    ]
    if dead:
        await _apply(runtime, {}, dead)

    computer_id = await _computer_id(workspace_id)
    rows = await list_computer_threads(computer_id) if computer_id else []
    mine = [row for row in rows if str(row["workspace_id"]) == workspace_id]
    tasks_by_thread: dict[str, list[dict[str, Any]]] = {}
    for task in await list_thread_tasks([str(r["conversation_thread_id"]) for r in mine]):
        tasks_by_thread.setdefault(str(task["thread_id"]), []).append(task)

    stale = []
    for row in mine:
        thread_id = str(row["conversation_thread_id"])
        tasks = tasks_by_thread.get(thread_id, [])
        prints = {t["task_id"]: _task_print(t) for t in tasks}
        if exported.get(thread_id[:8]) != _fingerprint(row["latest_checkpoint_id"], prints):
            stale.append((thread_id, row["latest_checkpoint_id"], tasks))

    gate = asyncio.Semaphore(_EXPORT_CONCURRENCY)
    failed = written = 0

    async def export(thread_id: str, checkpoint_id: str | None, tasks: list) -> None:
        nonlocal failed, written
        async with gate:
            try:
                previous = (
                    await _read_manifest(
                        runtime, f"{transcript_dir(layout, thread_id)}/{MANIFEST}"
                    )
                    if thread_id[:8] in exported
                    else {}
                )
                writes, removals = await plan_thread(
                    runtime,
                    layout,
                    thread_id,
                    checkpoint_id=checkpoint_id,
                    tasks=tasks,
                    previous=previous,
                )
                await _apply(runtime, writes, removals)
                written += bool(writes or removals)
            except Exception as e:
                failed += 1
                logger.warning(f"Transcript export failed for thread {thread_id}: {e}")

    await asyncio.gather(*(export(*item) for item in stale))
    if computer_id:
        await _apply(runtime, {index_path(layout.root): _index_content(layout.root, rows)}, [])
    return {"removed": len(dead), "exported": written, "failed": failed}


async def export_if_attached(thread_id: str) -> None:
    """Export a thread through this worker's session on its machine, if any.

    For a change that lands outside a turn (a background task finishing):
    another worker, or a stopped machine, is left to the next turn end or
    bring-up rather than woken for it.
    """
    from src.server.database.computer import get_computer_for_workspace
    from src.server.database.conversation import get_thread_by_id
    from src.server.services.workspace_manager import WorkspaceManager

    thread = await get_thread_by_id(thread_id)
    if not thread or not thread.get("workspace_id"):
        return
    workspace_id = str(thread["workspace_id"])
    computer = await get_computer_for_workspace(workspace_id)
    if not computer or not computer.get("provider_ref"):
        return
    session = WorkspaceManager.get_instance().get_session_if_ready(
        workspace_id, expected_sandbox_id=str(computer["provider_ref"])
    )
    runtime = getattr(getattr(session, "sandbox", None), "runtime", None)
    if runtime is None:
        return
    await export_thread(runtime, session.sandbox.working_dir, workspace_id, thread_id)


_exports: dict[str, asyncio.Task] = {}
_again: set[str] = set()


def schedule_thread_export(thread_id: str) -> None:
    """Export in the background, coalescing a burst (several tasks finishing
    together) into one export plus at most one rerun."""
    running = _exports.get(thread_id)
    if running is not None and not running.done():
        _again.add(thread_id)
        return

    async def _run() -> None:
        try:
            while True:
                _again.discard(thread_id)
                await export_if_attached(thread_id)
                if thread_id not in _again:
                    break
        except Exception as e:
            logger.warning(f"Transcript export failed for thread {thread_id}: {e}")
        finally:
            _exports.pop(thread_id, None)

    _exports[thread_id] = asyncio.create_task(_run())
