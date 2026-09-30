"""Which store route a mount path reaches, and what the mount may do there.

The daemon trusts these answers as a filesystem: a listing's size and
version must match what a read returns, a directory's writable flag decides
whether create is refused up front, and a refusal's code becomes the errno a
program sees. Past threads are renders of server state, so they refuse every
change, and a workspace path reaches only the user's own workspaces on this
computer. The
automations file is rows in Postgres, so its version is the route's own, not a
hash of the content, and a save takes its defaults from the command's context.
The sandbox links each directory in where the file tools show the same files.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from langgraph.store.memory import InMemoryStore

from ptc_agent.agent.backends import db_json_route
from ptc_agent.agent.backends.automations import AUTOMATIONS_FILE, AutomationsBackend
from ptc_agent.agent.backends.db_json_route import Plan
from ptc_agent.core.sandbox.livefs_mount import CallContext
from ptc_agent.core.sandbox.livefs_runtime.protocol import INLINE_MAX_BYTES
from src.server.database import workspace as workspace_db
from src.server.database.thread_transcripts import ListedFile, StoredFile
from src.server.database.workspace_folders import MOVING_DIR
from src.server.services.automations.file import AutomationsFile, Document, FilePlan, _Delete
from src.server.services.livefs.routes import LivefsError
from src.server.services.livefs.tokens import LivefsIdentity
from src.server.services.livefs.tree import LivefsTree

ROOT = "/home/workspace"
USER = "user-1"
COMPUTER = "computer-1"
HERE = "ws-here"  # on this computer, folder "research"
ELSEWHERE = "ws-elsewhere"  # the user's, on another computer
STRANGER = "ws-stranger"  # another user's
SECOND = "ws-second"  # on this computer, folder "second"
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
        (SECOND, USER, COMPUTER, "second"),
    )
}
T1 = "11111111-0000-0000-0000-000000000000"
T2 = "22222222-0000-0000-0000-000000000000"
SHORT = T1[:8]
THREADS = {HERE: T1, STRANGER: T2}
# Non-ASCII on purpose: a size counted in characters would pass on ASCII.
TRANSCRIPT = {
    "manifest.json": json.dumps({"thread_id": T1}, ensure_ascii=False),
    "turn-0001.jsonl": '{"role": "user", "content": "résumé 市场"}\n',
    "tasks/k1/meta.json": '{"description": "价格"}',
    "tasks/k1/run-0001.jsonl": '{"role": "ai", "content": "✓"}\n',
}
NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
AUTOMATIONS = "user/automations/automations.json"
SERVED = '{"automations": [{"name": "Morning brief", "status": "active"}]}\n'
EDITED = '{"automations": [{"name": "Morning brief", "status": "paused"}]}\n'
ROWS_VERSION = "sha256:rows-v1"
REPORT = "Saved automations.json: 1 updated.\nRead automations.json again before your next edit."


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
    for entry in (await tree.list(path)).entries:
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
        workspace_db, "get_workspace_placement", AsyncMock(side_effect=WORKSPACES.get)
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
def automations(monkeypatch):
    """The user's automations as the file serves and saves them. Every read,
    the save's own included, renders the file as ``content`` and ``version``
    stand at that moment, so a save leaves the file as a read serves it."""
    conn = MagicMock()

    @asynccontextmanager
    async def _open(*_, **__):
        yield conn

    conn.transaction = conn.cursor = _open
    monkeypatch.setattr(db_json_route, "get_db_connection", _open)
    rows = MagicMock(spec=AutomationsFile())
    rows.unchanged = None
    rows.content, rows.version = SERVED, ROWS_VERSION
    rows.fetch = AsyncMock(return_value=[])
    rows.render = MagicMock(side_effect=lambda _rows: (rows.content, rows.version))
    rows.parse = AsyncMock(return_value=Document(entries=[], states=None, timezone="UTC", model_pref=None))
    rows.plan = MagicMock(return_value=Plan(FilePlan()))
    rows.hold = AsyncMock(side_effect=lambda user_id, changes, planned, conn: rows.version if changes else None)
    rows.commit = AsyncMock(return_value=REPORT)
    monkeypatch.setitem(AutomationsBackend.files, AUTOMATIONS_FILE, rows)
    return rows


@pytest.fixture
def history(monkeypatch):
    """One stored thread per workspace in THREADS, as its rows hold it. Only
    the database is faked, so the service's own read path serves the files."""
    content = {path: text.encode() for path, text in TRANSCRIPT.items()}

    def workspace_transcripts(workspace_id):
        return [THREADS[workspace_id]] if workspace_id in THREADS else []

    def found(workspace_id, short_id):
        thread_id = THREADS.get(workspace_id)
        return bool(thread_id) and thread_id[:8] == short_id

    def list_transcript(workspace_id, short_id):
        if not found(workspace_id, short_id):
            return None
        return {path: (_sha(data), len(data)) for path, data in content.items()}

    def load_transcript_file(workspace_id, short_id, path):
        if not found(workspace_id, short_id) or path not in content:
            return None
        return StoredFile(USER, _sha(content[path]), content[path])

    def list_transcript_tree(workspace_id, short_id, limit):
        thread_id = THREADS.get(workspace_id)
        if thread_id is None or short_id not in (None, thread_id[:8]):
            return []
        listed = [
            ListedFile(thread_id, thread_id[:8], path, sha256, size)
            for path, (sha256, size) in sorted(list_transcript(workspace_id, thread_id[:8]).items())
        ]
        return listed[:limit]

    def load_transcript_contents(files):
        return {f: content[f.path] for f in files if _sha(content[f.path]) == f.sha256}

    mocks = SimpleNamespace(
        workspace_transcripts=AsyncMock(side_effect=workspace_transcripts),
        list_transcript=AsyncMock(side_effect=list_transcript),
        load_transcript_file=AsyncMock(side_effect=load_transcript_file),
        list_transcript_tree=AsyncMock(side_effect=list_transcript_tree),
        load_transcript_contents=AsyncMock(side_effect=load_transcript_contents),
    )
    for name, mock in vars(mocks).items():
        monkeypatch.setattr(f"src.server.database.thread_transcripts.{name}", mock)
    monkeypatch.setattr(
        "src.server.database.conversation.list_computer_threads",
        AsyncMock(side_effect=lambda _, rows=True: ("digest-1", [_thread_row(T1)] if rows else [])),
    )
    return mocks


# -- the layout ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_root_holds_user_workflows_workspaces_and_computer(tree):
    entries = (await tree.list("")).entries
    names = {e["name"] for e in entries}
    assert names == {"user", "workflows", "workspaces", "computer"}
    assert {e["type"] for e in entries} == {"dir"}


@pytest.mark.asyncio
async def test_user_holds_the_automations_folder_which_takes_no_new_files(tree, automations):
    entries = (await tree.list("user")).entries
    assert [e["name"] for e in entries] == ["memory", "memo", "profile", "automations"]

    listing = await tree.list("user/automations")
    assert [(e["name"], e["writable"]) for e in listing.entries] == [
        ("README.md", False),
        ("automations.json", True),
    ]
    assert listing.writable is False


@pytest.mark.asyncio
async def test_workspaces_lists_only_this_computers_live_workspaces(tree):
    entries = (await tree.list("workspaces")).entries
    assert [e["name"] for e in entries] == [HERE]


@pytest.mark.asyncio
async def test_a_workspace_path_serves_the_users_workspaces_on_this_computer(tree, store):
    store.put((USER, "workspaces", SECOND, "memory"), "plan.md", {"content": "p"})
    content, _, path = await tree.read(f"workspaces/{SECOND}/memory/plan.md")
    assert content == "p"
    assert path == f"{ROOT}/second/.agents/memory/plan.md"


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
    saved = await tree.write(
        f"workspaces/{HERE}/memory/notes.md",
        b"# notes",
        if_match=None,
        if_none_match="*",
    )
    assert saved.path == f"{ROOT}/research/.agents/memory/notes.md"
    assert saved.as_sent
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
    listings = {path: await tree.list(path) for path in directories}
    writable = {path for path, listing in listings.items() if listing.writable}
    assert writable == {"user/memory", "workflows", f"workspaces/{HERE}/memory"}
    # The daemon keeps a structural listing until the host runs ``link`` again,
    # so one a route serves (threads.jsonl moves under ``computer``) is not.
    structural = {path for path, listing in listings.items() if listing.structural}
    assert structural == {"", "user", "workspaces", f"workspaces/{HERE}"}


# -- where the sandbox links it -------------------------------------------------


def _live(monkeypatch, *folders: dict) -> None:
    monkeypatch.setattr(
        workspace_db,
        "get_live_workspace_folders_for_computer",
        AsyncMock(return_value=list(folders)),
    )


@pytest.mark.asyncio
async def test_each_live_folder_links_the_user_files_its_memory_and_transcripts(
    tree, monkeypatch
):
    """Bash runs in the folder, so the user's files are linked there too, at
    the relative path the file tools fold onto the root."""
    _live(monkeypatch, WORKSPACES[HERE], WORKSPACES[SECOND])

    links = await tree.links()

    here, second = f"{ROOT}/research/.agents", f"{ROOT}/second/.agents"
    assert links == {
        None: [
            ("user", f"{ROOT}/.agents/user"),
            ("workflows", f"{ROOT}/.agents/workflows"),
            ("computer/threads.jsonl", f"{ROOT}/.agents/threads.jsonl"),
        ],
        HERE: [
            ("user", f"{here}/user"),
            ("workflows", f"{here}/workflows"),
            (f"workspaces/{HERE}/memory", f"{here}/memory"),
            (f"workspaces/{HERE}/transcripts", f"{here}/transcripts"),
        ],
        SECOND: [
            ("user", f"{second}/user"),
            ("workflows", f"{second}/workflows"),
            (f"workspaces/{SECOND}/memory", f"{second}/memory"),
            (f"workspaces/{SECOND}/transcripts", f"{second}/transcripts"),
        ],
    }


@pytest.mark.asyncio
async def test_a_workspace_that_owns_the_root_shares_the_roots_user_link(tree, monkeypatch):
    _live(monkeypatch, {**WORKSPACES[HERE], "dir_name": None})

    links = await tree.links()

    assert [
        link for group in links.values() for link in group if link[0] == "user"
    ] == [("user", f"{ROOT}/.agents/user")]


@pytest.mark.asyncio
async def test_without_a_store_a_folder_links_its_transcripts_but_no_memory(tree):
    links = await LivefsTree(LivefsIdentity(COMPUTER, USER, ROOT), None).links()

    linked = {source for source, _ in links[HERE]}
    assert f"workspaces/{HERE}/transcripts" in linked
    assert f"workspaces/{HERE}/memory" not in linked


@pytest.mark.asyncio
async def test_a_folder_staged_mid_move_is_linked_once_it_lands(tree, monkeypatch):
    staged = {**WORKSPACES[SECOND], "dir_name": f"{MOVING_DIR}/{SECOND}"}
    _live(monkeypatch, WORKSPACES[HERE], staged)

    links = await tree.links()

    assert links.keys() == {None, HERE}
    assert all(MOVING_DIR not in target for group in links.values() for _, target in group)


# -- what a listing promises ---------------------------------------------------


class _SizeCache:
    """The Redis the thread index keeps its sizes in."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value, ex=None) -> bool:
        self.data[key] = str(value).encode()
        return True


@pytest.mark.asyncio
async def test_every_file_lists_the_size_version_and_content_its_read_returns(
    tree, store, history, automations, monkeypatch
):
    """The daemon keeps a file's bytes by (path, version) and skips the read
    when a listing names a version it holds, and takes a listed file's
    content in place of a read, so each route has to agree with its read."""
    from ptc_agent.agent.backends.user_data import UserDataBackend
    from ptc_agent.agent.backends.workflows import build_workflow_value
    from src.server.services.livefs import cache as livefs_cache

    big = "é" * (INLINE_MAX_BYTES // 2 + 1)
    store.put((USER, "memory"), "notes.md", {"content": "résumé 市场"})
    store.put((USER, "memory"), "big.md", {"content": big})
    store.put((USER, "workspaces", HERE, "memory"), "plan.md", {"content": "计划"})
    store.put((USER, "memos"), "brief.md", {"content": "kept ✓"})
    store.put((USER, "workflows"), "daily.js", build_workflow_value("run('✓')", None))

    def profile(filename: str) -> SimpleNamespace:
        return SimpleNamespace(
            fetch=AsyncMock(return_value=filename),
            render=lambda rows: (f'{{"file": "{rows}", "note": "价格"}}\n', f"rows:{rows}"),
        )

    for filename in list(UserDataBackend.files):
        monkeypatch.setitem(UserDataBackend.files, filename, profile(filename))
    sizes = _SizeCache()
    monkeypatch.setattr(
        livefs_cache, "get_cache_client", lambda: SimpleNamespace(enabled=True, client=sizes)
    )

    async def walk() -> dict:
        return {
            path: entry
            for top in ("user", "workflows", "workspaces", "computer")
            async for path, entry in _files(tree, top)
        }

    # Cold, the thread index is rendered for its size; warm, the size is kept.
    for _ in range(2):
        listed = await walk()
        transcripts = f"workspaces/{HERE}/transcripts/{SHORT}/"
        assert set(listed) == {
            "user/memory/notes.md",
            "user/memory/big.md",
            "user/memo/brief.md",
            *(f"user/profile/{name}" for name in (*UserDataBackend.data_files, "README.md")),
            "user/automations/README.md",
            "user/automations/automations.json",
            "workflows/daily.js",
            f"workspaces/{HERE}/memory/plan.md",
            *(transcripts + name for name in TRANSCRIPT),
            "computer/threads.jsonl",
        }
        for path, entry in listed.items():
            content, version, _ = await tree.read(path)
            assert entry["size"] == len(content.encode()), path
            assert entry["version"] == version, path
            assert entry.get("content", content) == content, path
    assert "content" not in listed["user/memory/big.md"]
    assert "content" not in listed["computer/threads.jsonl"]
    assert listed["user/profile/README.md"]["content"]


@pytest.mark.asyncio
async def test_a_transcript_listing_carries_every_folder_below_it_whole(tree, history):
    # The daemon answers the rest of the command from these, so a folder
    # missing a file hides it, and carried content stands in for its read.
    listing = await tree.list(f"workspaces/{HERE}/transcripts")

    assert [e["name"] for e in listing.entries] == [SHORT]
    assert set(listing.below) == {SHORT, f"{SHORT}/tasks", f"{SHORT}/tasks/k1"}
    assert not any(below["writable"] for below in listing.below.values())
    files = {
        f"{folder}/{e['name']}": e
        for folder, below in listing.below.items()
        for e in below["entries"]
        if e["type"] == "file"
    }
    assert set(files) == {f"{SHORT}/{path}" for path in TRANSCRIPT}
    for path, text in TRANSCRIPT.items():
        data = text.encode()
        entry = files[f"{SHORT}/{path}"]
        assert (entry["size"], entry["version"], entry["content"]) == (
            len(data),
            _sha(data)[:32],
            text,
        )


@pytest.mark.asyncio
async def test_a_thread_past_the_tree_limit_is_named_and_listed_on_its_own(
    tree, history, monkeypatch
):
    from src.server.services.livefs import history as history_route

    monkeypatch.setattr(history_route, "_TREE_MAX_FILES", len(TRANSCRIPT) - 1)
    root = f"workspaces/{HERE}/transcripts"

    listing = await tree.list(root)
    assert [e["name"] for e in listing.entries] == [SHORT]
    assert listing.below == {}
    thread = await tree.list(f"{root}/{SHORT}")
    assert [e["name"] for e in thread.entries] == ["manifest.json", "tasks", "turn-0001.jsonl"]
    assert thread.below == {}


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


# -- the automations file ------------------------------------------------------


async def _save(tree: LivefsTree, if_match: str, body: str = EDITED):
    return await tree.write(
        AUTOMATIONS, body.encode(), if_match=f'"{if_match}"', if_none_match=None
    )


@pytest.mark.asyncio
async def test_a_save_over_the_rows_version_lands_and_returns_its_report(
    tree, automations
):
    """A program writes the whole file, so what it leaves out is deleted even
    though no Read tool call stands behind it."""
    gone = _Delete(automation_id="id-1", name="Old job")
    automations.plan.return_value = Plan(FilePlan(deletes=[gone]), [gone.label])
    _, version, _ = await tree.read(AUTOMATIONS)
    assert version == ROWS_VERSION

    saved = await _save(tree, version)

    assert (saved.path, saved.report) == (
        f"{ROOT}/.agents/user/automations/automations.json",
        REPORT,
    )
    # Stored as the server renders it, which the sender has to read back.
    assert saved.size == len(SERVED.encode())
    assert not saved.as_sent
    automations.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_moving_under_a_read_never_makes_its_save_conflict(tree, automations):
    """A run moves ``state``, which the save ignores; only the rows' own
    version names a change the program did not see."""
    _, version, _ = await tree.read(AUTOMATIONS)
    automations.content = SERVED.replace("}]", ', "state": {"failure_count": 1}}]')

    saved = await _save(tree, version)

    assert saved.report == REPORT


@pytest.mark.asyncio
async def test_a_save_over_a_stale_version_is_refused_as_changed(tree, automations):
    refused = await _refusal(lambda: _save(tree, "sha256:rows-v0"))

    assert (refused.status, refused.code) == (412, "changed")
    automations.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_writer_landing_after_the_check_is_caught_under_the_route_lock(
    tree, automations
):
    automations.version = "sha256:rows-v2"

    refused = await _refusal(lambda: _save(tree, ROWS_VERSION))

    assert (refused.status, refused.code) == (412, "changed")
    automations.plan.assert_not_called()
    automations.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_server_failure_while_saving_is_refused_as_unavailable(tree, automations):
    automations.commit.side_effect = RuntimeError("connection reset")

    refused = await _refusal(lambda: _save(tree, ROWS_VERSION))

    assert (refused.status, refused.code) == (503, "unavailable")
    assert "nothing was saved" in refused.message


@pytest.mark.asyncio
async def test_a_save_answers_with_the_file_it_left_without_reading_it_back(
    tree, automations
):
    """Read back after the commit, a read that failed would answer a save
    that landed as NOT SAVED, and a program rerun on that would create its
    automations twice."""
    async def fetch(user_id, conn=None):
        if conn is None:
            raise RuntimeError("connection reset")
        return []

    automations.fetch.side_effect = fetch

    saved = await _save(tree, ROWS_VERSION)

    assert saved.report == REPORT
    assert (saved.version, saved.size) == (ROWS_VERSION, len(SERVED.encode()))


@pytest.mark.asyncio
async def test_the_automations_file_cannot_be_moved_away(tree, store, automations):
    """Refused before loading it, so a database outage cannot turn the
    answer into not found."""
    refused = await _refusal(
        lambda: tree.rename(AUTOMATIONS, "user/memory/automations.json")
    )

    assert (refused.status, refused.code) == (403, "read_only")
    assert store.search((USER, "memory")) == []
    automations.fetch.assert_not_awaited()
    automations.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "context",
    [CallContext(workspace_id=HERE, thread_id=T1, timezone="Asia/Tokyo"), None],
    ids=["filed", "nothing-filed"],
)
@pytest.mark.asyncio
async def test_a_save_takes_its_defaults_from_the_commands_context(store, automations, context):
    """Where a new automation runs, which thread ``"current"`` names, and its
    clock come from the conversation that ran the command."""
    tree = LivefsTree(LivefsIdentity(COMPUTER, USER, ROOT), store, context)

    await _save(tree, ROWS_VERSION)

    # A program sends no copy of what it read (None), so the rows stand for it.
    automations.parse.assert_awaited_once_with(USER, context or CallContext(), ANY, None)
    assert automations.plan.call_args.args[0] == (context or CallContext())
