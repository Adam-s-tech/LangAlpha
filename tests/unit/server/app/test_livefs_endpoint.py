"""The HTTP contract the sandbox file daemon is built against.

The daemon turns a refusal's ``code`` into an errno (401 and 429 by status
alone), sends back the ETag of a read as the next write's If-Match, and
counts on a provisional save's refusal staying out of the tool result. The
real tree runs over an in-memory store so each status comes from the same
route checks the file tools use; only the token row and Redis are faked.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langgraph.store.memory import InMemoryStore

from ptc_agent.agent.backends.langgraph_store import MAX_CONTENT_BYTES
from src.server.app import livefs as livefs_app
from src.server.app import setup as setup_mod
from src.server.services.livefs import outcomes, tokens
from tests.conftest import create_test_app

COMPUTER = "c0ffee00-0000-4000-8000-00000000000a"
OTHER_COMPUTER = "c0ffee00-0000-4000-8000-00000000000b"
USER = "user-livefs-endpoint-test"
ROOT = "/home/workspace"
SECRET = "unit-test-mount-secret"
TOKEN = f"lfs1.{COMPUTER}.{SECRET}"
CALL = "call0000test"

NOTES = "user/memory/notes.md"


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
    monkeypatch.setattr(livefs_app, "get_cache_client", lambda: _NoCache())
    app = create_test_app(livefs_app.router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as c:
        yield c


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
        client, NOTES, b"x" * (MAX_CONTENT_BYTES + 1), **{"If-None-Match": "*"}
    )

    assert _refusal(resp) == (413, "too_large")


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
    refusal's message reaches the agent through this report."""
    stale = await _make_stale(client)
    reported.clear()

    resp = await _put(client, NOTES, b"mine\n", **{"If-Match": f'"{stale}"'})

    assert [(call, o["op"], o["ok"], o["error"]) for call, o in reported] == [
        (CALL, "write", False, resp.json()["message"])
    ]


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


# -- rate limit --------------------------------------------------------------------


class _DownRedis:
    enabled = True

    def __init__(self) -> None:
        self.client = self

    def pipeline(self, **_):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_the_rate_limit_fails_open_when_redis_is_down(client, monkeypatch):
    """Every file op goes through here; a Redis outage must not stop them."""
    monkeypatch.setattr(livefs_app, "get_cache_client", lambda: _DownRedis())

    resp = await client.get("/api/v1/livefs/list", params={"path": ""})

    assert resp.status_code == 200
