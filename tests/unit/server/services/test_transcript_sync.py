"""Which agents get rendered into the store, and what a render keeps.

Rendering reads the whole checkpoint, so an agent whose stored copy is current
is never rendered again, a render older than the stored copy stands down
before it reads, and a task finishing reads its own run, not the thread's.
A save writes only the rows that changed. Compaction's live save replaces only
its own agent's copy and leaves a fingerprint that makes the next render from
the checkpoint redo it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ptc_agent.agent.transcript import TranscriptTarget, load_manifest
from ptc_agent.core.paths import SandboxLayout
from src.server.database.thread_transcripts import (
    StaleCopy,
    StoredTranscript,
    _changed_rows,
)
from src.server.services import transcripts
from src.server.services.computer_manager import _bringup
from src.server.services.transcripts import (
    INLINE_FILE_MAX_BYTES,
    Behind,
    _fingerprint,
    _Job,
    _render,
    _Target,
)

ROOT = "/home/workspace"
LAYOUT = SandboxLayout.for_root(ROOT).for_workspace("research")
T1 = "11111111-0000-0000-0000-000000000000"
T2 = "22222222-0000-0000-0000-000000000000"
FP = _fingerprint("cp-1")
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


class _Runtime:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.commands: list[str] = []

    async def exec(self, command: str):
        self.commands.append(command)
        return SimpleNamespace(stdout=self.stdout, exit_code=0)


def _target(*, inline: bool = False) -> _Target:
    return _Target("ws-1", "user-1", inline)


@pytest.fixture(autouse=True)
def folder(monkeypatch):
    """Where the workspace's folder is when a step reads it under its hold."""
    state = SimpleNamespace(layout=LAYOUT)

    @asynccontextmanager
    async def held(workspace_id, root):
        yield state.layout

    monkeypatch.setattr(
        "src.server.services.workspace_layout.held_workspace_layout", held
    )
    monkeypatch.setattr(_bringup, "held_workspace_layout", held)
    return state


def _stored(checkpoint_id: str = "cp-1", manifest: str = "{}", **files) -> StoredTranscript:
    return StoredTranscript(
        fingerprint=_fingerprint(checkpoint_id),
        checkpoint_id=checkpoint_id,
        manifest=manifest,
        files={
            name.replace("__", "/"): (sha, 10)
            for name, sha in (files or {"turn-0001.jsonl": "a" * 64}).items()
        },
    )


# -- which agents render -------------------------------------------------------


@pytest.fixture
def store():
    state = SimpleNamespace(in_store={}, tasks=[], renders=[])

    async def render(target, thread, stored):
        state.renders.append((thread.thread_id, thread.agents))
        return True

    async def stored_fingerprints(ids):
        return {t: fp for t, fp in state.in_store.items() if t in ids}

    async def load_stored(ids, prefix=None):
        return {}

    with (
        patch.object(transcripts, "_render", render),
        patch(
            "src.server.database.thread_transcripts.stored_fingerprints",
            stored_fingerprints,
        ),
        patch("src.server.database.thread_transcripts.load_stored", load_stored),
        patch(
            "src.server.database.runs.subagent_runs.list_thread_tasks",
            AsyncMock(side_effect=lambda ids: state.tasks),
        ),
    ):
        yield state


@pytest.mark.asyncio
async def test_a_thread_current_in_the_store_costs_nothing(store):
    store.in_store[T1] = {"": FP}
    counts = await transcripts._bring_in_line(_target(), [(T1, "cp-1")])
    assert counts == {"stored": 0, "failed": 0}
    assert store.renders == []


@pytest.mark.asyncio
async def test_only_threads_the_store_is_behind_on_render(store):
    store.in_store[T1] = {"": FP}
    store.in_store[T2] = {"": "stale"}
    counts = await transcripts._bring_in_line(_target(), [(T1, "cp-1"), (T2, "cp-1")])
    assert store.renders == [(T2, {""})]
    assert counts["stored"] == 1


@pytest.mark.asyncio
async def test_a_task_that_moved_renders_that_task_alone(store):
    task = {"thread_id": T1, "task_id": "k1", "latest_run_id": "r2", "status": "done"}
    store.tasks = [task]
    store.in_store[T1] = {"": FP, "tasks/k1/": "run r1"}
    await transcripts._bring_in_line(_target(), [(T1, "cp-1")])
    assert store.renders == [(T1, {"tasks/k1/"})]


@pytest.mark.asyncio
async def test_the_copy_of_a_task_a_truncation_deleted_is_dropped(monkeypatch, store):
    store.in_store[T1] = {"": FP, "tasks/k9/": "run r1"}
    [behind] = await transcripts.behind_in_store([(T1, "cp-1")])
    assert behind.agents == set() and behind.gone == {"tasks/k9/"}

    state = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "cp-1"}},
        values={"messages": [_message("hi")]},
    )
    reader = SimpleNamespace(aget_state=AsyncMock(return_value=state))
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: reader,
    )
    delete = AsyncMock()
    monkeypatch.setattr("src.server.database.thread_transcripts.delete_stored", delete)
    await _render(_target(), behind, {})
    delete.assert_awaited_once_with(T1, {"tasks/k9/"})


@pytest.mark.asyncio
async def test_a_render_older_than_the_stored_copy_stands_down_before_reading(monkeypatch):
    reader = SimpleNamespace(aget_state=AsyncMock())
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: reader,
    )
    save = AsyncMock()
    monkeypatch.setattr("src.server.database.thread_transcripts.save_stored", save)

    saved = await transcripts._render(
        _target(), Behind(T1, "cp-1", {}, {""}, set()), {"": _stored(checkpoint_id="cp-2")}
    )
    assert saved is False
    reader.aget_state.assert_not_awaited()
    save.assert_not_called()


@pytest.mark.asyncio
async def test_a_finished_task_renders_from_its_run_alone(monkeypatch):
    """The thread's own copy is current, so its checkpoint is not read, and
    the meta names the call that launched the run."""
    task = {
        "thread_id": T1,
        "task_id": "k1",
        "latest_run_id": "r1",
        "status": "completed",
        "launch_tool_call_id": "call-1",
    }
    meta = '{"schema": 2, "task_id": "k1", "launch_call_id": "call-1"}'
    reader = SimpleNamespace(
        aget_state=AsyncMock(),
        aget_task_history=AsyncMock(return_value=SimpleNamespace(messages=[_message("go")])),
    )
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: reader,
    )
    saves = []

    async def save_stored(thread_id, prefix, user_id, copy):
        saves.append((prefix, copy))
        return True

    monkeypatch.setattr("src.server.database.thread_transcripts.save_stored", save_stored)

    behind = Behind(T1, "cp-1", {"k1": task}, {"tasks/k1/"}, set())
    stored = {"": _stored(), "tasks/k1/": _stored(manifest=meta)}
    assert await _render(_target(inline=True), behind, stored)

    reader.aget_state.assert_not_awaited()
    [(prefix, copy)] = saves
    assert prefix == "tasks/k1/"
    assert load_manifest(copy.manifest)["launch_call_id"] == "call-1"


# -- what a save writes --------------------------------------------------------


def _copy(**files: str) -> StoredTranscript:
    return StoredTranscript(
        "fp", "cp-1", "{}", files={path: (sha * 64, 1) for path, sha in files.items()}
    )


def test_a_save_writes_only_the_rows_that_changed():
    copy = _copy(**{"turn-0001.jsonl": "a", "turn-0002.jsonl": "b", "turn-0003.jsonl": "c"})
    copy.carried = {"turn-0001.jsonl"}
    copy.inline = {"turn-0002.jsonl": b"b", "turn-0003.jsonl": b"c"}
    held = {
        "turn-0001.jsonl": ("a" * 64, "user-1", False),
        "turn-0002.jsonl": ("b" * 64, "user-1", True),
        "turn-0003.jsonl": ("x" * 64, "user-1", True),
    }
    rows = _changed_rows(T1, "", "user-1", copy, held)
    assert [row[2] for row in rows] == ["turn-0003.jsonl"]


def test_a_carried_file_whose_row_moved_is_a_stale_copy():
    copy = _copy(**{"turn-0001.jsonl": "a"})
    copy.carried = {"turn-0001.jsonl"}
    held = {"turn-0001.jsonl": ("z" * 64, "user-1", True)}
    with pytest.raises(StaleCopy):
        _changed_rows(T1, "", "user-1", copy, held)


def _job(messages) -> _Job:
    return _Job(TranscriptTarget(T1), messages, {"thread_id": T1}, FP, "cp-1", None)


def _big():
    from langchain_core.messages import AIMessage

    return AIMessage(content="x" * (INLINE_FILE_MAX_BYTES + 1), id="m-big")


def test_small_files_ride_the_rows_even_with_object_storage():
    rendered = _job([_message("hi"), _message("again"), _big()]).render(inline=False)
    assert set(rendered.copy.inline) == {"turn-0001.jsonl"}
    [(sha, data)] = rendered.blobs.items()
    assert rendered.copy.files["turn-0002.jsonl"] == (sha, len(data))


def test_without_object_storage_every_file_rides_the_rows():
    rendered = _job([_message("hi"), _big()]).render(inline=True)
    assert rendered.blobs == {}
    assert rendered.copy.inline.keys() == rendered.copy.files.keys()


# -- the index -----------------------------------------------------------------


def _row(thread_id: str, *, stored: bool = True, dir_name: str | None = "research"):
    return {
        "conversation_thread_id": thread_id,
        "title": "t",
        "workspace_id": "ws-1",
        "workspace_name": "Research",
        "dir_name": dir_name,
        "created_at": NOW,
        "updated_at": NOW,
        "has_transcript": stored,
    }


def test_the_index_names_a_path_only_for_a_stored_transcript():
    content = transcripts.index_content(
        ROOT,
        [_row(T1), _row(T2, stored=False), _row("33333333-0000", dir_name=None)],
    )
    lines = content.splitlines()
    assert len(lines) == 2
    assert f'"transcript": "{LAYOUT.transcripts}/{T1[:8]}"' in lines[0]
    assert '"transcript": null' in lines[1]


# -- the sync at bring-up ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_prune_clears_dead_dirs_and_copies_from_before_the_mount(
    monkeypatch,
):
    runtime = _Runtime(stdout=f"d {T1[:8]}\nt {T1[:8]}\nd 33333333\nr 33333333\ni\n")
    monkeypatch.setattr(
        "src.server.database.conversation.get_workspace_thread_short_ids",
        AsyncMock(return_value={T1[:8], T2[:8]}),
    )

    live = await transcripts.prune_dead_thread_dirs(runtime, LAYOUT, "ws-1")

    assert live == {T1[:8], T2[:8]}
    removal = next(c for c in runtime.commands if c.startswith("rm "))
    for path in (
        f"{LAYOUT.threads}/33333333",
        f"{LAYOUT.large_tool_results}/33333333",
        f"{LAYOUT.threads}/{T1[:8]}/transcript",
        transcripts.index_path(ROOT),
    ):
        assert path in removal
    assert f"{LAYOUT.threads}/{T1[:8]}" not in removal.split()


@pytest.mark.asyncio
async def test_the_sync_renders_the_threads_the_workspace_query_names(monkeypatch):
    seen = {}

    async def bring_in_line(target, threads):
        seen["threads"] = threads
        return {"stored": 0, "failed": 0}

    monkeypatch.setattr(transcripts, "_bring_in_line", bring_in_line)
    monkeypatch.setattr(transcripts, "_target", AsyncMock(return_value=_target()))
    monkeypatch.setattr(
        "src.server.database.thread_transcripts.workspace_checkpoints",
        AsyncMock(return_value=[(T1, "cp-1")]),
    )

    await transcripts.sync_workspace("ws-1")

    assert seen["threads"] == [(T1, "cp-1")]


@pytest.mark.asyncio
async def test_a_deleted_threads_dirs_leave_a_running_machine(monkeypatch):
    from src.server.services.computer_manager._bringup import BringUpMixin

    runtime = _Runtime(stdout=f"d {T1[:8]}\nd {T2[:8]}\nr {T2[:8]}\n")

    @asynccontextmanager
    async def computer_runtime(computer, sandbox_id):
        assert sandbox_id == "sb-1"
        yield runtime

    manager = SimpleNamespace(_computer_runtime=computer_runtime)
    monkeypatch.setattr(
        _bringup,
        "get_computer_for_workspace",
        AsyncMock(
            return_value={
                "computer_id": "c-1",
                "provider_ref": "sb-1",
                "status": "running",
                "root_dir": ROOT,
            }
        ),
    )
    monkeypatch.setattr(
        "src.server.database.conversation.get_workspace_thread_short_ids",
        AsyncMock(return_value={T1[:8]}),
    )

    await BringUpMixin.prune_thread_dirs_if_running(manager, "ws-1")

    assert runtime.commands[-1] == (
        f"rm -rf -- {LAYOUT.threads}/{T2[:8]} {LAYOUT.large_tool_results}/{T2[:8]}"
    )


# -- compaction's live save ----------------------------------------------------


def _message(text: str):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=text, id=f"m-{text}")


@pytest.fixture
def live(monkeypatch):
    """One thread's stored agents by prefix, and the saves that land."""
    state = SimpleNamespace(stored={}, saves=[])

    async def load_stored(ids, prefix=None):
        return {T1: {p: s for p, s in state.stored.items() if prefix in (None, p)}}

    async def save_stored(thread_id, prefix, user_id, copy):
        state.saves.append((prefix, copy))
        return True

    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_by_id",
        AsyncMock(return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        transcripts, "_target", AsyncMock(return_value=_target(inline=True))
    )
    monkeypatch.setattr("src.server.database.thread_transcripts.load_stored", load_stored)
    monkeypatch.setattr("src.server.database.thread_transcripts.save_stored", save_stored)
    return state


@pytest.mark.asyncio
async def test_a_live_save_keeps_the_rendered_checkpoint_and_empties_the_fingerprint(live):
    live.stored[""] = _stored(manifest='{"thread_id": "%s", "checkpoint_id": "cp-1"}' % T1)
    assert await transcripts.save_live(TranscriptTarget(T1), [_message("hi")])
    [(prefix, copy)] = live.saves
    assert prefix == ""
    assert copy.fingerprint == "" and copy.checkpoint_id == "cp-1"
    assert copy.files["turn-0001.jsonl"] != ("a" * 64, 10)
    assert load_manifest(copy.manifest)["checkpoint_id"] == "cp-1"


@pytest.mark.asyncio
async def test_a_live_save_of_a_task_replaces_only_that_task(live):
    live.stored["tasks/k1/"] = _stored(
        manifest='{"task_id": "k1", "description": "d"}',
        **{"tasks__k1__run-0001.jsonl": "b" * 64},
    )
    assert await transcripts.save_live(TranscriptTarget(T1, "k1"), [_message("go")])
    [(prefix, copy)] = live.saves
    assert prefix == "tasks/k1/"
    assert copy.files.keys() == {"tasks/k1/run-0001.jsonl"}
    assert load_manifest(copy.manifest)["description"] == "d"


@pytest.mark.asyncio
async def test_a_live_save_with_nothing_new_writes_nothing(live):
    assert await transcripts.save_live(TranscriptTarget(T1), [_message("hi")])
    [(_, first)] = live.saves
    live.stored[""] = first
    assert await transcripts.save_live(TranscriptTarget(T1), [_message("hi")])
    assert len(live.saves) == 1
