"""Each thread's transcript: JSONL the agent can grep, rendered from the
checkpoint into the store, where the computer's file mount serves it.

The checkpoint is the record and these files are a rendering of it, made when
it changes (a turn end, a task finishing) rather than when it is read. Each
agent of a thread (its own, and each background task's) is stored on its own
with a fingerprint of what it was rendered from, the thread's checkpoint or
the task's latest run, so an agent already current costs no state read and a
save touches only its agent's files. A render renders only the segments whose
shape changed since the stored copy, and a save writes only the rows that
differ. A small file's bytes sit in its row; a larger one's in the per-user
blob registry when there is object storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shlex
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ptc_agent.agent.transcript import (
    MANIFEST,
    TASK_META,
    TASKS_DIR,
    TranscriptTarget,
    build_directory,
    load_manifest,
    transcript_subdir,
)
from ptc_agent.agent.transcript.render import SCHEMA_VERSION
from ptc_agent.core.paths import THREAD_DIR_NAME, SandboxLayout, WorkspaceLayout

logger = logging.getLogger(__name__)

INDEX = "threads.jsonl"
# Where a thread's transcript sat, under its scratch dir, before the mount
# served them; a sync removes what is left of it.
LEGACY_TRANSCRIPT_DIR = "transcript"

# Files up to this size keep their bytes in the row even with object storage:
# most turns fit, and a row costs a turn end no upload and a read no fetch.
INLINE_FILE_MAX_BYTES = 64 * 1024

# Renders at once: each holds one checkpointer connection while it reads, and
# task reads and saves fan out below it.
_EXPORT_CONCURRENCY = 3
_TASK_READ_CONCURRENCY = 4
_SAVE_CONCURRENCY = 4
_BLOB_UPLOAD_CONCURRENCY = 8

# Blob bytes a read keeps, per process.
_READ_CACHE_BYTES = 64 * 1024 * 1024


def transcript_dir(layout: WorkspaceLayout, thread_id: str) -> str:
    return layout.join(transcript_subdir(thread_id[:8]))


def index_path(root: str) -> str:
    """The computer's thread index, beside every workspace folder rather than in one."""
    return f"{root.rstrip('/')}/{WorkspaceLayout.AGENTS_DIR}/{INDEX}"


def _task_print(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in ("latest_run_id", "status", "final_checkpoint_id")
    )


def _fingerprint(source: str | None) -> str:
    """An agent's copy is current while this matches: ``source`` is the
    thread's checkpoint for its own agent, a task's print for a task."""
    payload = json.dumps([SCHEMA_VERSION, source])
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _manifest_path(prefix: str) -> str:
    return prefix + (TASK_META if prefix else MANIFEST)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _newer(held: Any, candidate: Any) -> bool:
    """Whether a copy rendered at ``held`` is past ``candidate``. Checkpoint
    ids sort by time, so an export racing a newer one (another worker's turn
    end) stands down instead of undoing it."""
    return bool(held and candidate and held > candidate)


@dataclass
class _Target:
    """Whose store a render lands in."""

    workspace_id: str
    user_id: str
    #: No object storage: every file's bytes go in the rows.
    inline: bool


async def _target(workspace_id: str) -> _Target | None:
    from src.server.database.workspace import workspace_owner
    from src.utils.storage import is_storage_enabled

    try:
        user_id = await workspace_owner(workspace_id)
    except LookupError:
        return None
    return _Target(workspace_id, user_id, not is_storage_enabled())


@dataclass
class _Rendered:
    """One agent's copy as a render leaves it, and the blob bytes to store."""

    copy: Any
    blobs: dict[str, bytes]


@dataclass
class _Job:
    """One agent's render: what it renders from and the copy it replaces."""

    agent: TranscriptTarget
    messages: list[Any]
    header: dict[str, Any]
    fingerprint: str
    checkpoint_id: str | None
    #: The manifest of the stored copy this one replaces, if there is one.
    previous: str | None

    def render(self, *, inline: bool, full: bool = False) -> _Rendered:
        """The copy, each file named by digest. Only the segments that changed
        since ``previous`` render and carry bytes; the rest are carried over.
        CPU only, so it runs in a worker thread."""
        from src.server.database.thread_transcripts import StoredTranscript

        directory = build_directory(
            self.messages,
            unit=self.agent.unit,
            header=self.header,
            previous=None if full else load_manifest(self.previous),
        )
        copy = StoredTranscript(self.fingerprint, self.checkpoint_id, directory.manifest)
        blobs: dict[str, bytes] = {}
        for name, (sha, size) in directory.files.items():
            path = self.agent.prefix + name
            copy.files[path] = (sha, size)
            data = directory.rendered.get(name)
            if data is None:
                copy.carried.add(path)
            elif inline or size <= INLINE_FILE_MAX_BYTES:
                copy.inline[path] = data
            else:
                blobs[sha] = data
        return _Rendered(copy, blobs)


async def _store_blobs(user_id: str, contents: dict[str, bytes]) -> None:
    from src.server.database.workspace_file_blobs import registered_blobs, store_blobs

    if not contents:
        return
    registered = await registered_blobs(user_id, list(contents))
    await store_blobs(
        user_id,
        {sha: data for sha, data in contents.items() if sha not in registered},
        concurrency=_BLOB_UPLOAD_CONCURRENCY,
    )


async def _save(target: _Target, job: _Job, rendered: _Rendered) -> bool:
    """Store one agent's copy. One that carried files over from rows another
    save has since moved is rendered again in full."""
    from src.server.database.thread_transcripts import StaleCopy, save_stored

    agent = job.agent
    try:
        await _store_blobs(target.user_id, rendered.blobs)
        return await save_stored(agent.thread_id, agent.prefix, target.user_id, rendered.copy)
    except StaleCopy:
        if job.previous is None:
            raise
        logger.info(f"Transcript {agent.directory} moved under its render; rendering it in full")
    full = await asyncio.to_thread(job.render, inline=target.inline, full=True)
    await _store_blobs(target.user_id, full.blobs)
    return await save_stored(agent.thread_id, agent.prefix, target.user_id, full.copy)


@dataclass
class Behind:
    """A thread some of whose agents' stored copies are behind."""

    thread_id: str
    checkpoint_id: str | None
    #: Every task of the thread, by id, with its latest run.
    tasks: dict[str, dict[str, Any]]
    #: The agents to render, by prefix ("" for the thread's own).
    agents: set[str]
    #: Stored agents whose task is gone (a truncation deleted it).
    gone: set[str]


async def behind_in_store(threads: list[tuple[str, str | None]]) -> list[Behind]:
    """Which of these (thread id, checkpoint id) pairs the store is behind
    on, and on which of each thread's agents, in the order given. The store
    is read before the tasks, so a task stored here is one already created:
    missing from the tasks, it was deleted."""
    from src.server.database.runs.subagent_runs import list_thread_tasks
    from src.server.database.thread_transcripts import stored_fingerprints

    ids = [thread_id for thread_id, _ in threads]
    stored = await stored_fingerprints(ids)
    tasks: dict[str, dict[str, dict[str, Any]]] = {}
    for row in await list_thread_tasks(ids):
        tasks.setdefault(str(row["thread_id"]), {})[row["task_id"]] = row
    behind = []
    for thread_id, checkpoint_id in threads:
        mine = tasks.get(thread_id, {})
        wanted = {"": _fingerprint(checkpoint_id)} | {
            TranscriptTarget(thread_id, task_id).prefix: _fingerprint(_task_print(row))
            for task_id, row in mine.items()
        }
        have = stored.get(thread_id, {})
        agents = {prefix for prefix, print_ in wanted.items() if have.get(prefix) != print_}
        gone = have.keys() - wanted.keys()
        if agents or gone:
            behind.append(Behind(thread_id, checkpoint_id, mine, agents, gone))
    return behind


async def _render(target: _Target, thread: Behind, stored: dict[str, Any]) -> bool:
    """Render a thread's behind agents and replace their stored copies,
    ``stored`` being the headers the thread holds now by prefix, and drop the
    copies of tasks that are gone. The thread's checkpoint is read only when
    its own agent is behind, and only the runs of the tasks rendered are.
    Returns whether every save landed."""
    from src.server.database.thread_transcripts import delete_stored
    from src.server.services.history.reader import CheckpointHistoryReader

    thread_id = thread.thread_id
    own = stored.get("")
    if own is not None and _newer(own.checkpoint_id, thread.checkpoint_id):
        return False
    reader = CheckpointHistoryReader.get_instance()
    rendered_at = thread.checkpoint_id
    jobs: list[_Job] = []
    if "" in thread.agents:
        state = await reader.aget_state(thread_id, thread.checkpoint_id)
        rendered_at = (state.config or {}).get("configurable", {}).get("checkpoint_id")
        if own is not None and _newer(own.checkpoint_id, rendered_at):
            return False
        messages = list((state.values or {}).get("messages") or [])
        if not messages:
            # Nothing to show; a copy already stored is left for the next
            # render that has something, rather than emptied.
            return False
        jobs.append(
            _Job(
                TranscriptTarget(thread_id),
                messages,
                {"thread_id": thread_id, "checkpoint_id": rendered_at},
                _fingerprint(thread.checkpoint_id),
                rendered_at,
                own.manifest if own is not None else None,
            )
        )

    gate = asyncio.Semaphore(_TASK_READ_CONCURRENCY)

    async def read_task(task_id: str) -> list[Any]:
        async with gate:
            return (await reader.aget_task_history(thread_id, task_id)).messages

    task_ids = sorted(
        task_id
        for task_id in thread.tasks
        if TranscriptTarget(thread_id, task_id).prefix in thread.agents
    )
    for task_id, run_messages in zip(
        task_ids, await asyncio.gather(*(read_task(t) for t in task_ids))
    ):
        agent = TranscriptTarget(thread_id, task_id)
        row = thread.tasks[task_id]
        header = {
            "task_id": task_id,
            "description": row.get("description"),
            "subagent_type": row.get("subagent_type"),
            "status": row.get("status"),
            "created_at": _iso(row.get("created_at")),
            "launch_call_id": row.get("launch_tool_call_id"),
        }
        previous = stored.get(agent.prefix)
        jobs.append(
            _Job(
                agent,
                run_messages,
                header,
                _fingerprint(_task_print(row)),
                rendered_at,
                previous.manifest if previous is not None else None,
            )
        )

    def render_all() -> list[tuple[_Job, _Rendered]]:
        return [(job, job.render(inline=target.inline)) for job in jobs]

    saves = asyncio.Semaphore(_SAVE_CONCURRENCY)

    async def save(job: _Job, rendered: _Rendered) -> bool:
        async with saves:
            return await _save(target, job, rendered)

    ready = await asyncio.to_thread(render_all)
    landed = await asyncio.gather(*(save(job, rendered) for job, rendered in ready))
    if thread.gone:
        await delete_stored(thread_id, thread.gone)
    return all(landed)


async def _bring_in_line(
    target: _Target, threads: list[tuple[str, str | None]]
) -> dict[str, int]:
    """Render each thread whose stored copy is behind. ``threads`` are
    (thread id, checkpoint id), newest first."""
    from src.server.database.thread_transcripts import load_stored

    behind = await behind_in_store(threads)
    counts = {"stored": 0, "failed": 0}
    if not behind:
        return counts
    stored = await load_stored([thread.thread_id for thread in behind])
    gate = asyncio.Semaphore(_EXPORT_CONCURRENCY)

    async def one(thread: Behind) -> None:
        async with gate:
            try:
                saved = await _render(target, thread, stored.get(thread.thread_id, {}))
                counts["stored"] += saved
            except Exception as e:
                counts["failed"] += 1
                logger.warning(
                    f"Transcript export failed for thread {thread.thread_id}: {e}"
                )

    await asyncio.gather(*(one(thread) for thread in behind))
    return counts


async def export_thread(workspace_id: str, thread_id: str) -> dict[str, int]:
    """Bring one thread's stored transcript up to date: at turn end, when a
    task finishes, and for the backfill."""
    from src.server.database.conversation import get_thread_checkpoint_id

    checkpoint_id, target = await asyncio.gather(
        get_thread_checkpoint_id(thread_id), _target(workspace_id)
    )
    if target is None:
        return {"stored": 0, "failed": 0}
    return await _bring_in_line(target, [(thread_id, checkpoint_id)])


def index_content(root: str, rows: list[dict[str, Any]]) -> str:
    """The computer's thread index: one line per thread of a live folder,
    with where its transcript is, or null before its first turn ends."""
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
            "transcript": (
                transcript_dir(machine.for_workspace(row["dir_name"]), thread_id)
                if row.get("has_transcript")
                else None
            ),
        }
        lines.append(json.dumps(entry, ensure_ascii=False, default=str))
    return "".join(line + "\n" for line in lines)


def _manifest_prefix(path: str) -> str | None:
    """The agent whose manifest ``path`` is, by prefix; None for any other
    file. A manifest is its agent's header row, not a file row."""
    if path == MANIFEST:
        return ""
    head, _, name = path.rpartition("/")
    if name == TASK_META and head.startswith(f"{TASKS_DIR}/") and head.count("/") == 1:
        return f"{head}/"
    return None


async def list_files(workspace_id: str, short_id: str) -> dict[str, tuple[str, int]] | None:
    """Every file of a thread's stored transcript, by its path in the
    directory, with its sha256 and size; None when there is none."""
    from src.server.database.thread_transcripts import list_transcript

    listing = await list_transcript(workspace_id, short_id)
    if listing is None:
        return None
    return {
        _manifest_path(prefix) if path is None else path: (sha256, size)
        for prefix, path, sha256, size in listing
    } or None


class _VerifiedBytes:
    """Blob bytes already fetched and checked against their digest, by
    (user, sha256), within a byte budget, the least recently read going first.

    Bytes under a digest never change, so a copy here is never stale, and a
    read resolves the row naming the digest, which is what authorizes it,
    before asking. Concurrent misses for one digest share a fetch.
    """

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._size = 0
        self._held: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._fetching: dict[tuple[str, str], asyncio.Future[bytes]] = {}

    async def get(
        self, key: tuple[str, str], fetch: Callable[[], Awaitable[bytes]]
    ) -> bytes:
        data = self._held.get(key)
        if data is not None:
            self._held.move_to_end(key)
            return data
        pending = self._fetching.get(key)
        if pending is None or pending.get_loop() is not asyncio.get_running_loop():
            pending = asyncio.ensure_future(fetch())
            self._fetching[key] = pending
            pending.add_done_callback(lambda done: self._settle(key, done))
        # Shielded: a reader that goes away leaves the fetch to the others.
        return await asyncio.shield(pending)

    def _settle(self, key: tuple[str, str], done: asyncio.Future[bytes]) -> None:
        if self._fetching.get(key) is done:
            del self._fetching[key]
        if done.cancelled() or done.exception() is not None:
            return
        data = done.result()
        # One file never takes more than a quarter of the budget.
        if key in self._held or len(data) > self._budget // 4:
            return
        self._held[key] = data
        self._size += len(data)
        while self._size > self._budget:
            _, evicted = self._held.popitem(last=False)
            self._size -= len(evicted)


_verified = _VerifiedBytes(_READ_CACHE_BYTES)


async def read_file_with_digest(
    workspace_id: str, short_id: str, path: str
) -> tuple[bytes, str] | None:
    """One file of a thread's stored transcript, by its directory name and
    the path inside it, with the sha256 its listing gave; None when there is
    no such file. The digest names these exact bytes, so a caller needs no
    hash of its own."""
    from src.server.database.thread_transcripts import load_transcript_file
    from src.server.database.workspace_file_blobs import fetch_blob

    row = await load_transcript_file(
        workspace_id, short_id, path, manifest_of=_manifest_prefix(path)
    )
    if row is None:
        return None
    if row.content is not None:
        return row.content, row.sha256
    user_id, sha256 = row.user_id, row.sha256
    data = await _verified.get(
        (user_id, sha256), lambda: fetch_blob(user_id, sha256)
    )
    return data, sha256


async def read_file(workspace_id: str, short_id: str, path: str) -> bytes | None:
    """One file of a thread's stored transcript, by its directory name and
    the path inside it; None when there is no such file."""
    found = await read_file_with_digest(workspace_id, short_id, path)
    return found[0] if found is not None else None


_LIST_SCRIPT = """
for d in {threads}/*/; do
  [ -d "$d" ] || continue
  echo "d $(basename "$d")"
  [ -d "${{d}}{legacy}" ] && echo "t $(basename "$d")"
done
for d in {results}/*/; do
  [ -d "$d" ] && echo "r $(basename "$d")"
done
index={index}
[ -f "$index" ] && [ ! -L "$index" ] && echo "i"
true
"""


@dataclass
class ThreadDirListing:
    thread_dirs: set[str]
    #: Thread dirs still holding a transcript from before the mount.
    legacy: set[str]
    result_dirs: set[str]
    #: A written index sits where the mount links its own.
    legacy_index: bool


def listing_script(layout: WorkspaceLayout) -> str:
    """The shell that lists what ``prune_dead_thread_dirs`` judges, for a
    caller to run inside a script of its own and hand back parsed."""
    return _LIST_SCRIPT.format(
        threads=shlex.quote(layout.join(WorkspaceLayout.THREADS_DIR)),
        legacy=LEGACY_TRANSCRIPT_DIR,
        results=shlex.quote(layout.join(WorkspaceLayout.LARGE_TOOL_RESULTS_DIR)),
        index=shlex.quote(index_path(layout.root)),
    )


def parse_listing(stdout: str) -> ThreadDirListing:
    found = ThreadDirListing(set(), set(), set(), False)
    for line in stdout.splitlines():
        parts = line.split(" ")
        if parts[0] == "d" and len(parts) == 2:
            found.thread_dirs.add(parts[1])
        elif parts[0] == "t" and len(parts) == 2:
            found.legacy.add(parts[1])
        elif parts[0] == "r" and len(parts) == 2:
            found.result_dirs.add(parts[1])
        elif parts[0] == "i":
            found.legacy_index = True
    return found


async def prune_dead_thread_dirs(
    runtime: Any,
    layout: WorkspaceLayout,
    workspace_id: str,
    *,
    listing: ThreadDirListing | None = None,
) -> set[str]:
    """Remove the dirs of deleted threads and the transcripts written into the
    folder before the mount served them; return the live threads' short ids.

    A delete while the machine was down leaves its dirs behind. The listing
    runs before the thread read, so a thread created meanwhile is live by the
    time its dir could be judged; a caller passing ``listing`` ran
    ``listing_script`` before this call. The caller holds the folder
    throughout.
    """
    from src.server.database.conversation import get_workspace_thread_short_ids

    if listing is None:
        listed = await runtime.exec(listing_script(layout))
        listing = parse_listing(listed.stdout or "")
    live_short = await get_workspace_thread_short_ids(workspace_id)
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
    legacy = [
        f"{threads_dir}/{name}/{LEGACY_TRANSCRIPT_DIR}"
        for name in listing.legacy
        if name in live_short
    ]
    if listing.legacy_index:
        legacy.append(index_path(layout.root))
    if dead or legacy:
        await runtime.exec("rm -rf -- " + " ".join(shlex.quote(p) for p in dead + legacy))
        logger.info(
            f"Workspace {workspace_id}: removed {len(dead)} dead thread dirs and "
            f"{len(legacy)} legacy transcripts"
        )
    return live_short


async def sync_workspace(workspace_id: str) -> dict[str, int]:
    """Render the workspace's threads whose stored copy is behind their
    checkpoint (a turn end whose render failed, a thread from before
    transcripts were stored), newest first.
    """
    from src.server.database.thread_transcripts import workspace_checkpoints

    target = await _target(workspace_id)
    counts = {"stored": 0, "failed": 0}
    if target is not None:
        # A thread with no stamped checkpoint has not finished a turn; its
        # first turn end exports it.
        counts = await _bring_in_line(target, await workspace_checkpoints(workspace_id))
    return counts


async def save_live(transcript: TranscriptTarget, messages: list[Any]) -> bool:
    """Store one agent's transcript from messages in hand, ahead of the render
    from the checkpoint: compaction points the model at it mid-turn, before
    the turn end renders it. Only the segments that changed since the stored
    copy render. The copy keeps the checkpoint its stored one was rendered
    at, so it never lands over a newer render, and an empty fingerprint: the
    checkpoint the turn ends at does not exist yet, so the turn end's render
    still reads it, and renders only what came after this save. Returns
    whether it landed or had nothing to change."""
    from src.server.database.conversation import get_thread_by_id
    from src.server.database.thread_transcripts import load_stored

    thread_id = transcript.thread_id
    thread = await get_thread_by_id(thread_id)
    if not thread or not thread.get("workspace_id"):
        return False
    target = await _target(str(thread["workspace_id"]))
    if target is None:
        return False
    prefix = transcript.prefix
    stored = (await load_stored([thread_id], prefix)).get(thread_id, {}).get(prefix)
    held = load_manifest(stored.manifest) if stored is not None else {}
    header = {k: v for k, v in held.items() if k not in ("schema", "segments")}
    if transcript.task_id is None:
        header["thread_id"] = thread_id
    else:
        header["task_id"] = transcript.task_id
    job = _Job(
        transcript,
        messages,
        header,
        "",
        stored.checkpoint_id if stored is not None else None,
        stored.manifest if stored is not None else None,
    )
    rendered = await asyncio.to_thread(job.render, inline=target.inline)
    if stored is not None and rendered.copy.manifest == stored.manifest:
        return True
    return await _save(target, job, rendered)


async def _export_by_id(thread_id: str, workspace_id: str | None) -> None:
    from src.server.database.conversation import get_thread_by_id

    if workspace_id is None:
        thread = await get_thread_by_id(thread_id)
        workspace_id = str(thread["workspace_id"]) if thread and thread.get("workspace_id") else None
    if workspace_id is not None:
        await export_thread(workspace_id, thread_id)


# One export of a thread at a time across workers. The worker holding the
# lock exports until the flag is down; one that finds it held only raises the
# flag, and the holder checks it again after letting go, so an export asked
# for mid-render still runs. Both keys expire: a worker that dies holding the
# lock delays the thread's next export rather than stopping it.
_LOCK_TTL_S = 300
_FLAG_TTL_S = 3600
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _redis() -> Any:
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
    except Exception:
        return None
    return cache.client if getattr(cache, "enabled", False) and cache.client else None


async def _export_once(thread_id: str, workspace_id: str | None) -> None:
    client = _redis()
    lock = f"transcripts:export:{thread_id}"
    flag = f"transcripts:export-again:{thread_id}"
    holder = uuid.uuid4().hex
    while True:
        try:
            if client is not None:
                await client.set(flag, "1", ex=_FLAG_TTL_S)
                if not await client.set(lock, holder, nx=True, ex=_LOCK_TTL_S):
                    return
        except Exception as e:
            logger.warning(f"Transcript export of {thread_id} runs unlocked: {e}")
            client = None
        if client is None:
            await _export_by_id(thread_id, workspace_id)
            return
        try:
            while await client.delete(flag):
                await _export_by_id(thread_id, workspace_id)
        finally:
            with contextlib.suppress(Exception):
                await client.eval(_RELEASE_LUA, 1, lock, holder)
        if not await client.exists(flag):
            return


_exports: dict[str, asyncio.Task] = {}
_again: set[str] = set()


def schedule_thread_export(thread_id: str, workspace_id: str | None = None) -> None:
    """Export in the background, coalescing a burst (several tasks finishing
    together) into one export plus at most one rerun, and one worker at a
    time. At a turn end, and for a change that lands outside a turn."""
    running = _exports.get(thread_id)
    if running is not None and not running.done():
        _again.add(thread_id)
        return

    async def _run() -> None:
        try:
            while True:
                _again.discard(thread_id)
                await _export_once(thread_id, workspace_id)
                if thread_id not in _again:
                    break
        except Exception as e:
            logger.warning(f"Transcript export failed for thread {thread_id}: {e}")
        finally:
            _exports.pop(thread_id, None)

    _exports[thread_id] = asyncio.create_task(_run())
