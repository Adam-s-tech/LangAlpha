"""Bringing a computer's file mount up, and when a warm turn asks again.

The mount links each live workspace folder and the computer's thread index
into the sandbox, and it is asked for again only when something is due, since
every ask is an exec into the sandbox. A mount failure never costs a turn: the
file tools still reach these files through the store.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ptc_agent.core.paths import SandboxLayout
from src.server.services import transcripts
from src.server.services.computer_manager import ComputerManager
from src.server.services.computer_manager import _provisioning
from src.server.services.livefs import mount
from src.server.services.livefs.mount import MountState
from src.server.services.livefs.tokens import MintedToken

ROOT = "/home/workspace"
LAYOUT = SandboxLayout.for_root(ROOT)
INDEX = transcripts.index_path(ROOT)
COMPUTER = "comp-test-1"
USER = "user-test-1"
WS_A = "ws-test-a"
WS_B = "ws-test-b"
DEAD = "cat: notes.md: Transport endpoint is not connected"


def _in(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


class _Sandbox:
    """What ``livefs_mount.up`` touches, answering each ``up`` in turn."""

    def __init__(self, *answers: dict, sandbox_id: str = "sb-test-1") -> None:
        self.sandbox_id = sandbox_id
        self.layout = LAYOUT
        self.livefs = None
        self.uploads: list[tuple[str, bytes]] = []
        self.commands: list[str] = []
        self._answers = list(answers) or [{"ok": True, "started": True}]
        self.runtime = SimpleNamespace(upload_file=self._upload, exec_as_root=self._exec)

    async def _runtime_call(self, func, *args, retry_policy):
        return await func(*args)

    async def _upload(self, data: bytes, path: str) -> None:
        self.uploads.append((path, data))

    async def _exec(self, command: str, timeout: int):
        self.commands.append(command)
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return SimpleNamespace(stdout=json.dumps(answer), stderr="")

    def argv(self, index: int = -1) -> list[str]:
        return shlex.split(self.commands[index])


@pytest.fixture
def server(monkeypatch):
    """The server's side of a bring-up, with its database and store stood in for."""
    from src.server.app import setup

    state = SimpleNamespace(
        top=["user", "workflows", "workspaces", "computer"],
        folders=[{"workspace_id": WS_A, "dir_name": "research-a"}],
        expires_at=_in(59),
        mint=AsyncMock(return_value=MintedToken("lfs1.comp-test-1.fresh", _in(60))),
        settling=False,
    )

    class _Tree:
        def __init__(self, identity, store) -> None:
            pass

        async def list(self, path):
            return [{"name": name, "type": "dir"} for name in state.top], False

    @asynccontextmanager
    async def publish_lock(computer_id):
        yield

    @asynccontextmanager
    async def workspace_folders_lock(computer_id):
        yield None if state.settling else object()

    monkeypatch.setattr(setup, "store", object())
    monkeypatch.setattr(mount, "LivefsTree", _Tree)
    monkeypatch.setattr(
        mount.workspace_db,
        "get_live_workspace_folders_for_computer",
        AsyncMock(side_effect=lambda _computer_id: list(state.folders)),
    )
    monkeypatch.setattr(mount.db, "publish_lock", publish_lock)
    monkeypatch.setattr(mount, "workspace_folders_lock", workspace_folders_lock)
    monkeypatch.setattr(
        mount.db, "token_expiry", AsyncMock(side_effect=lambda _c: state.expires_at)
    )
    monkeypatch.setattr(mount.tokens, "mint_token", state.mint)
    monkeypatch.setattr(mount, "effective_relay_base_url", lambda _p: "http://relay.test")
    return state


async def _ensure(sandbox: _Sandbox, **kwargs) -> MountState:
    return await mount.ensure(
        sandbox, computer_id=COMPUTER, user_id=USER, provider="daytona", **kwargs
    )


# -- what gets linked ----------------------------------------------------------


@pytest.mark.asyncio
async def test_each_live_folder_links_its_memory_and_transcripts_then_the_index(server):
    server.folders.append({"workspace_id": WS_B, "dir_name": "research-b"})

    links, workspace_ids = await mount._links(COMPUTER, USER, LAYOUT)

    a, b = LAYOUT.for_workspace("research-a"), LAYOUT.for_workspace("research-b")
    assert links[2:] == [
        (f"workspaces/{WS_A}/memory", a.memory),
        (f"workspaces/{WS_A}/transcripts", a.transcripts),
        (f"workspaces/{WS_B}/memory", b.memory),
        (f"workspaces/{WS_B}/transcripts", b.transcripts),
        ("computer/threads.jsonl", INDEX),
    ]
    assert workspace_ids == {WS_A, WS_B}


@pytest.mark.asyncio
async def test_without_a_store_a_folder_links_its_transcripts_but_no_memory(
    server, monkeypatch
):
    from src.server.app import setup

    monkeypatch.setattr(setup, "store", None)
    server.top = []

    links, workspace_ids = await mount._links(COMPUTER, USER, LAYOUT)

    assert links == [
        (f"workspaces/{WS_A}/transcripts", LAYOUT.for_workspace("research-a").transcripts),
        ("computer/threads.jsonl", INDEX),
    ]
    assert workspace_ids == {WS_A}


@pytest.mark.asyncio
async def test_a_folder_staged_mid_move_is_linked_once_it_lands(server):
    server.folders.append({"workspace_id": WS_B, "dir_name": f"_internal/moving/{WS_B}"})

    links, workspace_ids = await mount._links(COMPUTER, USER, LAYOUT)

    assert all(f"workspaces/{WS_B}/" not in source for source, _ in links)
    assert workspace_ids == {WS_A}


@pytest.mark.asyncio
async def test_a_settle_holding_the_folders_leaves_the_mount_alone(server):
    server.settling = True
    sandbox = _Sandbox()

    state = await _ensure(sandbox)

    assert not state.mounted and state.error == "busy"
    assert sandbox.commands == []


@pytest.mark.asyncio
async def test_a_computer_with_nothing_to_link_is_left_unmounted(server):
    server.top, server.folders = [], []
    sandbox = _Sandbox()

    state = await _ensure(sandbox)

    assert not state.mounted and state.reason == "nothing to mount"
    assert sandbox.commands == []


@pytest.mark.asyncio
async def test_up_links_the_index_over_the_one_a_sync_used_to_write(server):
    sandbox = _Sandbox()

    state = await _ensure(sandbox)

    argv = sandbox.argv()
    assert argv[argv.index("--replace") + 1] == INDEX
    assert f"computer/threads.jsonl:{INDEX}" in argv
    assert state.mounted and state.workspace_ids == {WS_A}


# -- the token the sandbox holds -----------------------------------------------


@pytest.mark.asyncio
async def test_a_warm_ensure_keeps_a_token_with_time_left(server):
    sandbox = _Sandbox()

    state = await _ensure(sandbox)

    server.mint.assert_not_awaited()
    assert sandbox.uploads == [] and "--stage" not in sandbox.argv()
    assert state.expires_at == server.expires_at


@pytest.mark.asyncio
async def test_bring_up_rewrites_a_token_that_still_has_time_left(server):
    sandbox = _Sandbox()

    await _ensure(sandbox, fresh_token=True)

    ((_, data),) = sandbox.uploads
    assert json.loads(data) == {
        "base_url": "http://relay.test",
        "token": "lfs1.comp-test-1.fresh",
    }


@pytest.mark.asyncio
async def test_a_sandbox_that_lost_its_token_is_given_a_new_one(server):
    sandbox = _Sandbox({"ok": False, "error": "no_config"}, {"ok": True})

    state = await _ensure(sandbox)

    assert state.mounted
    server.mint.assert_awaited_once()
    assert "--stage" not in sandbox.argv(0) and "--stage" in sandbox.argv(1)


@pytest.mark.asyncio
async def test_a_staged_token_is_removed_even_when_the_command_dies(server):
    sandbox = _Sandbox()

    await _ensure(sandbox, fresh_token=True)

    ((staged, _),) = sandbox.uploads
    assert staged.startswith(f"{LAYOUT.internal}/.livefs.") and staged.endswith(".stage")
    argv = sandbox.argv()
    assert argv[argv.index("--stage") + 1] == staged
    assert sandbox.commands[-1].endswith(f"; rc=$?; rm -f {staged}; exit $rc")


# -- failures ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_never_raises_when_the_sandbox_call_fails(server, caplog):
    sandbox = _Sandbox()
    sandbox.runtime.exec_as_root = AsyncMock(side_effect=RuntimeError("exec lost"))

    state = await _ensure(sandbox)

    assert not state.mounted and state.reason == "exec lost"
    assert "livefs mount failed" in caplog.text


@pytest.mark.asyncio
async def test_a_refused_mount_is_logged_and_reported_with_the_daemons_code(
    server, caplog
):
    sandbox = _Sandbox({"ok": False, "error": "unsupported", "reason": "no /dev/fuse"})

    state = await _ensure(sandbox)

    assert not state.mounted
    assert (state.error, state.reason) == ("unsupported", "no /dev/fuse")
    assert "unsupported" in caplog.text


# -- the due check on a warm turn ----------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    """The manager's monotonic clock, set by hand; the event loop keeps its own."""
    now = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(
        _provisioning,
        "time",
        SimpleNamespace(monotonic=lambda: now.t, time=time.time),
    )
    return now


def _manager() -> ComputerManager:
    return ComputerManager(SimpleNamespace(sandbox=SimpleNamespace(provider="docker")))


def _mounted(*workspace_ids: str, minutes_left: float = 59) -> MountState:
    return MountState(True, _in(minutes_left), workspace_ids=frozenset(workspace_ids))


async def _saw(manager, sandbox, state: MountState) -> None:
    """Let the manager record one ensure whose answer is ``state``."""
    with patch.object(mount, "ensure", AsyncMock(return_value=state)):
        await manager._ensure_livefs(COMPUTER, USER, sandbox)


@pytest.mark.asyncio
async def test_a_serving_mount_on_the_same_sandbox_is_not_asked_again(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, _mounted(WS_A, minutes_left=31))

    assert sandbox.livefs is not None
    assert not manager._livefs_due(COMPUTER, WS_A, sandbox)


@pytest.mark.asyncio
async def test_a_different_sandbox_is_due(clock):
    manager = _manager()
    await _saw(manager, _Sandbox(), _mounted(WS_A))

    rebuilt = _Sandbox(sandbox_id="sb-test-2")
    assert manager._livefs_due(COMPUTER, WS_A, rebuilt)


@pytest.mark.asyncio
async def test_a_workspace_not_yet_linked_is_due(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, _mounted(WS_A))

    assert manager._livefs_due(COMPUTER, WS_B, sandbox)


@pytest.mark.asyncio
async def test_a_token_under_thirty_minutes_is_due(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, _mounted(WS_A, minutes_left=29))

    assert manager._livefs_due(COMPUTER, WS_A, sandbox)


@pytest.mark.asyncio
async def test_a_reconnected_sandbox_object_without_the_mount_handle_is_due(clock):
    manager = _manager()
    await _saw(manager, _Sandbox(), _mounted(WS_A))

    reconnected = _Sandbox()
    assert reconnected.livefs is None
    assert manager._livefs_due(COMPUTER, WS_A, reconnected)


@pytest.mark.asyncio
async def test_a_failed_mount_is_retried_after_ten_minutes(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, MountState(False, error="unreachable"))

    assert sandbox.livefs is None
    clock.t += 599
    assert not manager._livefs_due(COMPUTER, WS_A, sandbox)
    clock.t += 1
    assert manager._livefs_due(COMPUTER, WS_A, sandbox)


@pytest.mark.asyncio
async def test_a_sandbox_installing_fuse_is_retried_after_twenty_seconds(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, MountState(False, error="installing"))

    clock.t += 19
    assert not manager._livefs_due(COMPUTER, WS_A, sandbox)
    clock.t += 1
    assert manager._livefs_due(COMPUTER, WS_A, sandbox)


@pytest.mark.asyncio
async def test_a_mount_kept_busy_by_a_settle_serves_on_and_is_asked_after_twenty_seconds(
    clock,
):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, _mounted(WS_A))
    handle = sandbox.livefs

    await _saw(manager, sandbox, MountState(False, error="busy"))

    assert sandbox.livefs is handle
    clock.t += 19
    assert not manager._livefs_due(COMPUTER, WS_B, sandbox)
    clock.t += 1
    assert manager._livefs_due(COMPUTER, WS_B, sandbox)


@pytest.mark.asyncio
async def test_an_unsupported_sandbox_is_never_retried_but_its_rebuild_is(clock):
    manager, sandbox = _manager(), _Sandbox()
    await _saw(manager, sandbox, MountState(False, error="unsupported"))

    clock.t += 10**9
    assert not manager._livefs_due(COMPUTER, WS_A, sandbox)
    assert manager._livefs_due(COMPUTER, WS_A, _Sandbox(sandbox_id="sb-test-2"))


# -- the handle the file tools hold --------------------------------------------


@pytest.mark.asyncio
async def test_parallel_commands_share_one_token_refresh():
    calls = []

    async def refresh():
        calls.append(1)
        await asyncio.sleep(0)
        return _mounted(WS_A, minutes_left=60)

    handle = mount.Handle(COMPUTER, _mounted(WS_A, minutes_left=5), refresh)

    await asyncio.gather(handle.prepare(), handle.prepare())

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_ordinary_command_never_touches_the_daemon(monkeypatch):
    monkeypatch.setattr(mount.outcomes, "collect", AsyncMock(return_value=[]))
    refresh = AsyncMock()
    handle = mount.Handle(COMPUTER, _mounted(WS_A), refresh)

    assert await handle.report("c0ffee00c0ffee00", "ok\n") == ""
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_command_that_met_a_dead_mount_restarts_it_and_says_so(monkeypatch):
    monkeypatch.setattr(mount.outcomes, "collect", AsyncMock(return_value=[]))
    refresh = AsyncMock(return_value=MountState(True, started=True))
    handle = mount.Handle(COMPUTER, _mounted(WS_A), refresh)

    text = await handle.report("c0ffee00c0ffee00", DEAD)

    refresh.assert_awaited_once()
    assert "serving again; rerun the command" in text


@pytest.mark.asyncio
async def test_a_dead_mount_already_restarted_elsewhere_adds_no_note(monkeypatch):
    monkeypatch.setattr(mount.outcomes, "collect", AsyncMock(return_value=[]))
    refresh = AsyncMock(return_value=MountState(True, started=False))
    handle = mount.Handle(COMPUTER, _mounted(WS_A), refresh)

    assert await handle.report("c0ffee00c0ffee00", DEAD) == ""
