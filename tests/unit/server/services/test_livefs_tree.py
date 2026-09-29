"""Which store route a mount path reaches, and what the mount may do there.

The daemon trusts these answers as a filesystem: a listing's size and
version must match what a read returns, a directory's writable flag decides
whether create is refused up front, and a refusal's code becomes the errno a
program sees. Past threads are renders of server state, so they refuse every
change, and a workspace path reaches only the user's own workspaces on this
computer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.store.memory import InMemoryStore

from src.server.database import workspace as workspace_db
from src.server.database.thread_transcripts import StoredFile
from src.server.services.livefs.tokens import LivefsIdentity
from src.server.services.livefs.tree import LivefsError, LivefsTree

ROOT = "/home/workspace"
USER = "user-1"
COMPUTER = "computer-1"
HERE = "ws-here"  # on this computer, folder "research"
ELSEWHERE = "ws-elsewhere"  # the user's, on another computer
STRANGER = "ws-stranger"  # another user's
WORKSPACES = {
    workspace_id: {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "computer_id": computer_id,
        "dir_name": dir_name,
    }
    for workspace_id, user_id, computer_id, dir_name in (
        (HERE, USER, COMPUTER, "research"),
        (ELSEWHERE, USER, "computer-2", "notes"),
        (STRANGER, "user-2", COMPUTER, "theirs"),
    )
}
T1 = "11111111-0000-0000-0000-000000000000"
T2 = "22222222-0000-0000-0000-000000000000"
SHORT = T1[:8]
THREADS = {HERE: T1, STRANGER: T2}
# Non-ASCII on purpose: a size counted in characters would pass on ASCII.
TRANSCRIPT = {
    "turn-0001.jsonl": '{"role": "user", "content": "résumé 市场"}\n',
    "tasks/k1/meta.json": '{"description": "价格"}',
    "tasks/k1/run-0001.jsonl": '{"role": "ai", "content": "✓"}\n',
}
MANIFEST = json.dumps({"thread_id": T1}, ensure_ascii=False)
# A manifest is its agent's header row; every other file is a file row.
MANIFESTS = {"": MANIFEST, "tasks/k1/": TRANSCRIPT["tasks/k1/meta.json"]}
ROWS = {path: text for path, text in TRANSCRIPT.items() if not path.endswith(".json")}
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _thread_row(thread_id: str) -> dict:
    return {
        "conversation_thread_id": thread_id,
        "title": "市场研究",
        "workspace_id": HERE,
        "workspace_name": "Research",
        "dir_name": "research",
        "created_at": NOW,
        "updated_at": NOW,
        "has_transcript": True,
    }


async def _refusal(attempt) -> LivefsError:
    with pytest.raises(LivefsError) as caught:
        await attempt()
    return caught.value


async def _files(tree: LivefsTree, path: str):
    """Every file under ``path`` with the listing entry it was found by."""
    entries, _ = await tree.list(path)
    for entry in entries:
        child = f"{path}/{entry['name']}"
        if entry["type"] == "dir":
            async for found in _files(tree, child):
                yield found
        else:
            yield child, entry


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def tree(store, monkeypatch):
    monkeypatch.setattr(
        workspace_db, "get_workspace", AsyncMock(side_effect=WORKSPACES.get)
    )
    monkeypatch.setattr(
        workspace_db,
        "get_live_workspace_folders_for_computer",
        AsyncMock(return_value=[{**WORKSPACES[HERE], "name": "Research"}]),
    )
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: SimpleNamespace(enabled=True),
    )
    return LivefsTree(LivefsIdentity(COMPUTER, USER, ROOT), store)


@pytest.fixture
def history(monkeypatch):
    """One stored thread per workspace in THREADS, as its rows hold it. Only
    the database is faked, so the service's own read path serves the files."""
    content = {path: text.encode() for path, text in ROWS.items()}

    def workspace_transcripts(workspace_id):
        return [THREADS[workspace_id]] if workspace_id in THREADS else []

    def found(workspace_id, short_id):
        thread_id = THREADS.get(workspace_id)
        return bool(thread_id) and thread_id[:8] == short_id

    def list_transcript(workspace_id, short_id):
        if not found(workspace_id, short_id):
            return None
        manifests = [
            (prefix, None, _sha(text.encode()), len(text.encode()))
            for prefix, text in MANIFESTS.items()
        ]
        files = [
            (path.rpartition("/")[0] + "/" if "/" in path else "", path, _sha(data), len(data))
            for path, data in content.items()
        ]
        return manifests + files

    def load_transcript_file(workspace_id, short_id, path, *, manifest_of=None):
        if not found(workspace_id, short_id):
            return None
        if manifest_of is not None:
            text = MANIFESTS.get(manifest_of)
            return None if text is None else StoredFile(None, _sha(text.encode()), text.encode())
        if path not in content:
            return None
        return StoredFile(USER, _sha(content[path]), content[path])

    mocks = SimpleNamespace(
        workspace_transcripts=AsyncMock(side_effect=workspace_transcripts),
        list_transcript=AsyncMock(side_effect=list_transcript),
        load_transcript_file=AsyncMock(side_effect=load_transcript_file),
    )
    for name, mock in vars(mocks).items():
        monkeypatch.setattr(f"src.server.database.thread_transcripts.{name}", mock)
    monkeypatch.setattr(
        "src.server.database.conversation.list_computer_threads",
        AsyncMock(return_value=[_thread_row(T1)]),
    )
    return mocks


# -- the layout ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_root_holds_user_workflows_workspaces_and_computer(tree):
    entries, _ = await tree.list("")
    names = {e["name"] for e in entries}
    assert names == {"user", "workflows", "workspaces", "computer"}
    assert {e["type"] for e in entries} == {"dir"}


@pytest.mark.asyncio
async def test_workspaces_lists_only_this_computers_live_workspaces(tree):
    entries, _ = await tree.list("workspaces")
    assert [e["name"] for e in entries] == [HERE]


@pytest.mark.asyncio
async def test_the_users_workspace_on_another_computer_is_not_found(tree, store, history):
    store.put((USER, "workspaces", ELSEWHERE, "memory"), "plan.md", {"content": "p"})
    for attempt in (
        lambda: tree.read(f"workspaces/{ELSEWHERE}/memory/plan.md"),
        lambda: tree.list(f"workspaces/{ELSEWHERE}/transcripts"),
        lambda: tree.write(
            f"workspaces/{ELSEWHERE}/memory/new.md", b"x", if_match=None, if_none_match="*"
        ),
    ):
        assert (await _refusal(attempt)).code == "not_found"
    history.workspace_transcripts.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_users_workspace_is_not_found(tree, history):
    # Transcripts are keyed by workspace alone, so ownership is the only fence.
    for path in (
        f"workspaces/{STRANGER}",
        f"workspaces/{STRANGER}/transcripts",
        f"workspaces/{STRANGER}/transcripts/{T2[:8]}",
        "workspaces/ws-missing",
    ):
        assert (await _refusal(lambda: tree.list(path))).code == "not_found"
    history.workspace_transcripts.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_memory_is_saved_under_that_workspaces_folder(tree, store):
    _, _, saved = await tree.write(
        f"workspaces/{HERE}/memory/notes.md",
        b"# notes",
        if_match=None,
        if_none_match="*",
    )
    assert saved == f"{ROOT}/research/.agents/memory/notes.md"
    item = store.get((USER, "workspaces", HERE, "memory"), "notes.md")
    assert item.value["content"] == "# notes"
    assert store.search((USER, "memory")) == []


@pytest.mark.asyncio
async def test_new_files_are_accepted_only_where_the_file_tools_write(tree, history):
    # The daemon refuses create up front in the rest, since a refusal that
    # only arrives at close() goes unseen by most programs.
    directories = (
        "",
        "user",
        "user/memory",
        "user/memo",
        "workflows",
        "workspaces",
        f"workspaces/{HERE}",
        f"workspaces/{HERE}/memory",
        f"workspaces/{HERE}/transcripts",
        f"workspaces/{HERE}/transcripts/{SHORT}",
        "computer",
    )
    writable = {path for path in directories if (await tree.list(path))[1]}
    assert writable == {"user/memory", "workflows", f"workspaces/{HERE}/memory"}


# -- what a listing promises ---------------------------------------------------


@pytest.mark.asyncio
async def test_past_threads_list_the_size_and_version_their_read_returns(tree, history):
    listed = [
        found
        for top in (f"workspaces/{HERE}/transcripts", "computer")
        async for found in _files(tree, top)
    ]
    prefix = f"workspaces/{HERE}/transcripts/{SHORT}/"
    assert {path for path, _ in listed} == {
        *(prefix + name for name in (*TRANSCRIPT, "manifest.json")),
        "computer/threads.jsonl",
    }
    for path, entry in listed:
        content, version, _ = await tree.read(path)
        assert entry["size"] == len(content.encode()), path
        assert entry["version"] == version, path
        assert entry["writable"] is False


@pytest.mark.asyncio
async def test_a_transcript_directory_name_must_be_a_short_thread_id(tree, history):
    for name in (".git", SHORT[:7], SHORT + "0", "ABCDEF12"):
        path = f"workspaces/{HERE}/transcripts/{name}"
        assert (await _refusal(lambda: tree.list(path))).code == "not_found"
    history.list_transcript.assert_not_awaited()


# -- refusals ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_threads_refuse_every_change_as_read_only(tree, history):
    turn = f"workspaces/{HERE}/transcripts/{SHORT}/turn-0001.jsonl"
    _, turn_version, _ = await tree.read(turn)
    _, index_version, _ = await tree.read("computer/threads.jsonl")
    attempts = (
        lambda: tree.write(turn, b"x", if_match=turn_version, if_none_match=None),
        lambda: tree.write(
            f"workspaces/{HERE}/transcripts/{SHORT}/new.md",
            b"x",
            if_match=None,
            if_none_match="*",
        ),
        lambda: tree.delete(turn),
        lambda: tree.rename(turn, "user/memory/turn.jsonl"),
        lambda: tree.write(
            "computer/threads.jsonl", b"x", if_match=index_version, if_none_match=None
        ),
    )
    for attempt in attempts:
        refused = await _refusal(attempt)
        assert (refused.status, refused.code) == (403, "read_only")


@pytest.mark.asyncio
async def test_memos_refuse_changes_as_read_only(tree, store):
    store.put((USER, "memos"), "brief.md", {"content": "kept"})
    _, version, _ = await tree.read("user/memo/brief.md")
    for attempt in (
        lambda: tree.write(
            "user/memo/brief.md", b"changed", if_match=version, if_none_match=None
        ),
        lambda: tree.delete("user/memo/brief.md"),
    ):
        refused = await _refusal(attempt)
        assert (refused.status, refused.code) == (403, "read_only")
    assert (await tree.read("user/memo/brief.md"))[0] == "kept"


@pytest.mark.asyncio
async def test_dot_segments_are_refused_before_any_route_is_reached(tree, history):
    for path in (
        f"user/memory/../../workspaces/{STRANGER}/memory/x.md",
        f"workspaces/./{HERE}/memory/x.md",
        f"workspaces/{HERE}/transcripts/{SHORT}/../../memory/x.md",
    ):
        refused = await _refusal(lambda: tree.read(path))
        assert (refused.status, refused.code) == (422, "invalid")
    history.load_transcript_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_name_the_store_cannot_key_is_refused_as_invalid(tree, store):
    refused = await _refusal(
        lambda: tree.write(
            "user/memory/bad name!.md", b"x", if_match=None, if_none_match="*"
        )
    )
    assert (refused.status, refused.code) == (422, "invalid")
    assert store.search((USER, "memory")) == []
