"""The HTTP contract the sandbox file daemon is built against.

The daemon turns a refusal's ``code`` into an errno (401 and 429 by status
alone), sends back the ETag of a read as the next write's If-Match, and
counts on a provisional save's refusal staying out of the tool result. A save
runs for the conversation its command filed, and what it changed reaches the
tool result. The real tree runs over an in-memory store so each status comes
from the same route checks the file tools use; only the token row, Redis and
the automations rows are faked.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langgraph.store.memory import InMemoryStore

from ptc_agent.agent.backends import db_json_route
from ptc_agent.agent.backends.automations import AUTOMATIONS_FILE, AutomationsBackend
from ptc_agent.agent.backends.db_json_route import Plan
from ptc_agent.agent.backends.langgraph_store import MAX_CONTENT_BYTES
from ptc_agent.core.sandbox.livefs_mount import CallContext
from ptc_agent.core.sandbox.livefs_runtime.protocol import MAX_FILE_BYTES
from src.server.app import livefs as livefs_app
from src.server.app import setup as setup_mod
from src.server.database import user as user_db
from src.server.services.automations.file import AutomationsFile, Document, FilePlan
from src.server.services.livefs import cache, outcomes, tokens
from tests.conftest import create_test_app

COMPUTER = "c0ffee00-0000-4000-8000-00000000000a"
OTHER_COMPUTER = "c0ffee00-0000-4000-8000-00000000000b"
USER = "user-livefs-endpoint-test"
ROOT = "/home/workspace"
SECRET = "unit-test-mount-secret"
TOKEN = f"lfs1.{COMPUTER}.{SECRET}"
CALL = "call0000test"

NOTES = "user/memory/notes.md"
AUTOMATIONS = "user/automations/automations.json"
SERVED = '{"automations": [{"name": "Morning brief", "status": "active"}]}\n'
ROWS_VERSION = "sha256:rows-v1"
REPORT = "Saved automations.json: 1 updated.\n- updated Morning brief"
WORKSPACE = "00000000-0000-4000-8000-00000000aaaa"
THREAD = "00000000-0000-4000-8000-00000000bbbb"


class _Tokens:
    """The token row ``authenticate`` compares against: one live computer."""

    async def load_token(self, computer_id):
        if computer_id != COMPUTER:
            return None
        return {
            "user_id": USER,
            "token_sha256": hashlib.sha256(SECRET.encode()).digest(),
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "prev_token_sha256": None,
            "prev_expires_at": None,
            "root_dir": ROOT,
        }


class _NoCache:
    enabled = False
    client = None


@pytest.fixture
def reported(monkeypatch) -> list[tuple[str | None, dict]]:
    """The save outcomes filed for tool results, as (call id, outcome)."""
    filed: list[tuple[str | None, dict]] = []

    async def record(computer_id, call_id, outcome):
        filed.append((call_id, outcome))

    monkeypatch.setattr(outcomes, "record", record)
    return filed


@pytest_asyncio.fixture
async def client(monkeypatch, reported):
    monkeypatch.setattr(setup_mod, "store", InMemoryStore(), raising=False)
    monkeypatch.setattr(tokens, "db", _Tokens())
    monkeypatch.setattr(cache, "get_cache_client", lambda: _NoCache())
    # No command filed who it runs for, so a save runs for no conversation.
    monkeypatch.setattr(outcomes, "call_context", AsyncMock(return_value=None))
    app = create_test_app(livefs_app.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as c:
        yield c


@pytest.fixture
def parse(monkeypatch) -> AsyncMock:
    """The automations rows' save, planned and committed as nothing changing;
    the rows themselves never change, so the save settles on what was served.
    The save's context reaches the file first where it parses the document,
    which this returns."""
    conn = MagicMock()

    @asynccontextmanager
    async def _open(*_, **__):
        yield conn

    conn.transaction = conn.cursor = _open
    monkeypatch.setattr(db_json_route, "get_db_connection", _open)
    file = MagicMock(spec=AutomationsFile())
    file.unchanged = None
    file.fetch = AsyncMock(return_value=[])
    file.render = MagicMock(return_value=(SERVED, ROWS_VERSION))
    file.parse = AsyncMock(return_value=Document(entries=[], states=None, timezone="UTC", model_pref=None))
    file.plan = MagicMock(return_value=Plan(FilePlan()))
    file.hold = AsyncMock(return_value=None)
    file.commit = AsyncMock(return_value=REPORT)
    monkeypatch.setitem(AutomationsBackend.files, AUTOMATIONS_FILE, file)
    return file.parse


async def _put(client, path, body: bytes, **headers):
    return await client.put(
        "/api/v1/livefs/write",
        params={"path": path},
        content=body,
        headers={"X-Livefs-Call": CALL, **headers},
    )


async def _create(client, path, text: str) -> str:
    resp = await _put(client, path, text.encode(), **{"If-None-Match": "*"})
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


async def _read(client, path):
    return await client.get("/api/v1/livefs/read", params={"path": path})


async def _save_automations(client):
    resp = await _put(
        client, AUTOMATIONS, SERVED.encode(), **{"If-Match": f'"{ROWS_VERSION}"'}
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _make_stale(client) -> str:
    """Create NOTES, change it, and return the version it had before."""
    stale = await _create(client, NOTES, "first\n")
    changed = await _put(client, NOTES, b"elsewhere\n", **{"If-Match": f'"{stale}"'})
    assert changed.status_code == 200, changed.text
    return stale


def _refusal(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["code"]


# -- versions --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_etag_a_read_returns_is_accepted_as_the_next_writes_if_match(client):
    await _create(client, NOTES, "first\n")

    read = await _read(client, NOTES)
    assert read.status_code == 200
    assert read.content == b"first\n"

    resp = await _put(client, NOTES, b"second\n", **{"If-Match": read.headers["etag"]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["as_sent"] is True
    assert (await _read(client, NOTES)).headers["etag"] == f'"{resp.json()["version"]}"'


@pytest.mark.asyncio
async def test_a_write_naming_a_stale_version_is_refused_as_changed(client):
    stale = await _make_stale(client)

    resp = await _put(client, NOTES, b"mine\n", **{"If-Match": f'"{stale}"'})

    assert _refusal(resp) == (412, "changed")
    assert (await _read(client, NOTES)).content == b"elsewhere\n"


@pytest.mark.asyncio
async def test_creating_a_file_that_already_exists_is_refused_as_exists(client):
    await _create(client, NOTES, "first\n")

    resp = await _put(client, NOTES, b"again\n", **{"If-None-Match": "*"})

    assert _refusal(resp) == (412, "exists")


@pytest.mark.asyncio
async def test_a_write_without_a_precondition_is_refused_and_saves_nothing(client):
    """A blind write would land over a change the daemon never saw."""
    resp = await _put(client, NOTES, b"blind\n")

    assert _refusal(resp) == (428, "precondition_required")
    assert (await _read(client, NOTES)).status_code == 404


# -- refusals ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_memo_write_is_refused_as_read_only(client):
    resp = await _put(client, "user/memo/brief.md", b"x", **{"If-None-Match": "*"})

    assert _refusal(resp) == (403, "read_only")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("user/memory/../memo/brief.md", b"x"),
        (NOTES, b"\xff\xfe not utf-8"),
        ("user/profile/notes.txt", b"{}"),
    ],
    ids=["traversal", "non-utf8", "unknown-profile-file"],
)
@pytest.mark.asyncio
async def test_a_bad_path_body_or_file_name_is_refused_as_invalid(client, path, body):
    resp = await _put(client, path, body, **{"If-None-Match": "*"})

    assert _refusal(resp) == (422, "invalid")


@pytest.mark.asyncio
async def test_a_body_over_the_file_cap_is_refused_as_too_large(client):
    resp = await _put(
        client, NOTES, b"x" * (MAX_FILE_BYTES + 1), **{"If-None-Match": "*"}
    )

    assert _refusal(resp) == (413, "too_large")


def test_the_daemon_refuses_a_write_at_the_cap_the_store_keeps():
    # A larger daemon cap accepts the write and fails it at close instead.
    assert MAX_FILE_BYTES == MAX_CONTENT_BYTES


@pytest.mark.parametrize(
    "authorization",
    [
        f"Bearer lfs1.{COMPUTER}.forged-secret",
        f"Bearer lfs1.{OTHER_COMPUTER}.{SECRET}",
        "Bearer lfs1.not-a-uuid.secret",
        "",
    ],
    ids=["forged", "moved", "malformed", "empty"],
)
@pytest.mark.asyncio
async def test_a_bad_token_is_refused_with_401(client, authorization):
    resp = await client.get(
        "/api/v1/livefs/read",
        params={"path": NOTES},
        headers={"Authorization": authorization},
    )

    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


# -- save outcomes -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_save_is_reported_under_its_call_id(client, reported):
    """A save through the mount fails at close() with only an errno, so the
    refusal's message reaches the agent through this report, by the sandbox
    path it used."""
    stale = await _make_stale(client)
    reported.clear()

    resp = await _put(client, NOTES, b"mine\n", **{"If-Match": f'"{stale}"'})

    assert [(call, o["op"], o["path"], o["ok"], o["error"]) for call, o in reported] == [
        (CALL, "write", f"{ROOT}/.agents/user/memory/notes.md", False, resp.json()["message"])
    ]


@pytest.mark.asyncio
async def test_a_refusal_where_nothing_is_linked_names_the_mount_path(client, reported):
    resp = await _put(client, "elsewhere/notes.md", b"x", **{"If-None-Match": "*"})

    assert _refusal(resp) == (404, "not_found")
    assert [o["path"] for _, o in reported] == ["/mnt/livefs/elsewhere/notes.md"]


@pytest.mark.asyncio
async def test_a_refused_provisional_save_is_not_reported(client, reported):
    """The empty save a shell's ``> file`` makes before its command writes;
    the daemon retries it at release, and only that retry is reported."""
    stale = await _make_stale(client)
    reported.clear()

    resp = await _put(
        client, NOTES, b"", **{"If-Match": f'"{stale}"', "X-Livefs-Provisional": "1"}
    )

    assert _refusal(resp) == (412, "changed")
    assert reported == []


@pytest.mark.asyncio
async def test_what_a_save_changed_reaches_its_outcome_but_not_the_log(
    client, reported, parse, caplog
):
    """The report names the user's automations; the tool result is its only
    reader."""
    caplog.set_level(logging.INFO, logger=livefs_app.logger.name)

    await _save_automations(client)

    assert reported == [
        (
            CALL,
            {
                "op": "write",
                "path": f"{ROOT}/.agents/user/automations/automations.json",
                "ok": True,
                "size": len(SERVED.encode()),
                "report": REPORT,
            },
        )
    ]
    (logged,) = [r for r in caplog.records if r.getMessage() == "livefs write"]
    assert logged.ok is True and not hasattr(logged, "report")


# -- who a save runs for -----------------------------------------------------------


@pytest.mark.parametrize("timezone", ["Asia/Tokyo", None], ids=["filed-clock", "no-clock"])
@pytest.mark.asyncio
async def test_a_save_runs_for_the_workspace_thread_and_clock_its_command_filed(
    client, parse, monkeypatch, timezone
):
    """A command that filed no clock leaves it unset, for the automations
    file to fill from the user's own before the save."""
    context = CallContext(workspace_id=WORKSPACE, thread_id=THREAD, timezone=timezone)
    filed = AsyncMock(return_value=context)
    monkeypatch.setattr(outcomes, "call_context", filed)

    await _save_automations(client)

    filed.assert_awaited_once_with(COMPUTER, CALL)
    assert parse.await_args.args[:2] == (USER, context)


@pytest.mark.asyncio
async def test_a_save_no_command_filed_runs_for_no_conversation(client, parse):
    await _save_automations(client)

    assert parse.await_args.args[:2] == (USER, CallContext())


@pytest.mark.asyncio
async def test_the_endpoint_never_looks_up_the_user(client, parse, monkeypatch):
    """The user's own clock is the automations file's to read, and only when
    the command filed none, so no other save waits on, or fails with, the
    users table."""
    get_user = AsyncMock(side_effect=RuntimeError("database down"))
    monkeypatch.setattr(user_db, "get_user", get_user)

    await _create(client, NOTES, "first")
    await _save_automations(client)

    get_user.assert_not_awaited()


# -- rate limit --------------------------------------------------------------------


class _DownRedis:
    enabled = True

    def __init__(self) -> None:
        self.client = self

    async def eval(self, *_):
        raise ConnectionError("redis down")

    async def get(self, *_):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_the_rate_limit_fails_open_when_redis_is_down(client, monkeypatch):
    """Every file op goes through here; a Redis outage must not stop them."""
    monkeypatch.setattr(cache, "get_cache_client", lambda: _DownRedis())

    resp = await client.get("/api/v1/livefs/list", params={"path": ""})

    assert resp.status_code == 200
    assert resp.json()["structural"] is True


@pytest.mark.asyncio
async def test_a_throttled_request_runs_nothing_and_its_command_is_told(client, monkeypatch):
    """Refused before the handler, so the daemon's retry cannot land a save
    twice, and noted for the command's tool result, since the program sees
    only EAGAIN."""
    identity = tokens.LivefsIdentity(COMPUTER, USER, ROOT)
    throttle = AsyncMock(side_effect=tokens.LivefsThrottled(identity, 3))
    monkeypatch.setattr(livefs_app, "authenticate", throttle)
    noted = AsyncMock()
    monkeypatch.setattr(outcomes, "throttled", noted)

    resp = await _put(client, NOTES, b"x", **{"If-None-Match": "*"})

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3"
    noted.assert_awaited_once_with(COMPUTER, CALL)
    monkeypatch.setattr(livefs_app, "authenticate", tokens.authenticate)
    assert (await _read(client, NOTES)).status_code == 404
