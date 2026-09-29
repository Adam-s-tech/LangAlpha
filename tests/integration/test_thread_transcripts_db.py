"""Which save of an agent's stored transcript lands, against real PostgreSQL.

A turn end, another worker's turn end and compaction's live save can all
write one agent's copy at once, so the header row's upsert condition decides
the winner in a single statement; only Postgres can show that it does. Each
test makes its own user, workspace and thread and removes them afterwards.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.server.database.thread_transcripts import (
    LIVE_FINGERPRINT,
    StoredTranscript,
    save_stored,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Checkpoint ids sort by the time they were taken.
CP_OLD = "1f000000-0000-6000-8000-000000000001"
CP_NEW = "1f000000-0000-6000-8000-000000000002"
TASK = "tasks/k1/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy(checkpoint_id: str | None = CP_OLD, *, inline=None, files=None, live=False):
    """A stored copy; ``inline`` files carry their bytes, ``files`` only a sha."""
    inline = inline or {}
    listed = {path: (_sha(data), len(data)) for path, data in inline.items()}
    listed.update(files or {})
    return StoredTranscript(
        fingerprint=LIVE_FINGERPRINT if live else f"fp-{checkpoint_id}",
        checkpoint_id=checkpoint_id,
        manifest="{}",
        files=listed,
        inline=dict(inline),
    )


@pytest_asyncio.fixture
async def thread(patched_get_db_connection, test_db_pool):
    from src.server.database.conversation import create_thread
    from src.server.database.user import create_user
    from src.server.database.workspace import create_workspace

    user_id = f"transcript-test-{uuid.uuid4().hex[:12]}"
    await create_user(user_id=user_id, email=f"{user_id}@example.com", name="Test")
    workspace = await create_workspace(user_id=user_id, name="Transcripts", status="running")
    workspace_id = str(workspace["workspace_id"])
    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=workspace_id,
        current_status="completed",
    )

    async def read(prefix: str = ""):
        """One agent's header row, and every file row of the thread."""
        async with test_db_pool.connection() as conn:
            header = await (
                await conn.execute(
                    "SELECT checkpoint_id, fingerprint FROM thread_transcripts "
                    "WHERE conversation_thread_id = %s AND prefix = %s",
                    (thread_id, prefix),
                )
            ).fetchone()
            rows = await (
                await conn.execute(
                    "SELECT path, sha256, content FROM thread_transcript_files "
                    "WHERE conversation_thread_id = %s",
                    (thread_id,),
                )
            ).fetchall()
        files = {
            r["path"]: (r["sha256"], None if r["content"] is None else bytes(r["content"]))
            for r in rows
        }
        return header, files

    try:
        yield SimpleNamespace(id=thread_id, user_id=user_id, read=read)
    finally:
        async with test_db_pool.connection() as conn:
            await conn.execute(
                "DELETE FROM conversation_threads WHERE workspace_id = %s", (workspace_id,)
            )
            await conn.execute("DELETE FROM workspaces WHERE workspace_id = %s", (workspace_id,))
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


async def _save(thread, copy: StoredTranscript, prefix: str = "") -> bool:
    return await save_stored(thread.id, prefix, thread.user_id, copy)


# -- racing saves --------------------------------------------------------------


async def test_a_save_older_than_the_stored_checkpoint_loses(thread):
    await _save(thread, _copy(CP_NEW, inline={"turn-0001.jsonl": b"new"}))

    assert not await _save(thread, _copy(CP_OLD, inline={"turn-0001.jsonl": b"old"}))

    header, files = await thread.read()
    assert header["checkpoint_id"] == CP_NEW
    assert files["turn-0001.jsonl"][1] == b"new"


@pytest.mark.parametrize("checkpoint_id", [CP_OLD, CP_NEW], ids=["same", "newer"])
async def test_a_save_at_or_after_the_stored_checkpoint_replaces_it(thread, checkpoint_id):
    await _save(thread, _copy(CP_OLD))

    assert await _save(thread, _copy(checkpoint_id, inline={"turn-0001.jsonl": b"x"}))

    header, files = await thread.read()
    assert header["fingerprint"] == f"fp-{checkpoint_id}"
    assert files["turn-0001.jsonl"][1] == b"x"


@pytest.mark.parametrize(
    ("stored", "saved"), [(None, CP_OLD), (CP_NEW, None)], ids=["stored", "saved"]
)
async def test_a_null_checkpoint_on_either_side_lets_the_save_land(thread, stored, saved):
    await _save(thread, _copy(stored))

    assert await _save(thread, _copy(saved))

    header, _ = await thread.read()
    assert header["checkpoint_id"] == saved


# -- compaction's live save ----------------------------------------------------


async def test_a_render_at_a_live_copys_checkpoint_leaves_this_turn_in_place(thread):
    """Mid-turn, the thread's checkpoint is still the last turn's end; a
    render from there lacks the turn the live copy holds."""
    await _save(thread, _copy(inline={"turn-0001.jsonl": b"a"}))
    turn = {"turn-0001.jsonl": b"a", "turn-0002.jsonl": b"so far"}
    assert await _save(thread, _copy(inline=turn, live=True))

    assert not await _save(thread, _copy(inline={"turn-0001.jsonl": b"a"}))

    header, files = await thread.read()
    assert header["fingerprint"] == LIVE_FINGERPRINT
    assert files["turn-0002.jsonl"][1] == b"so far"


@pytest.mark.parametrize(
    ("prefix", "live", "replacement"),
    [
        ("", CP_OLD, _copy(CP_OLD, live=True)),
        ("", CP_OLD, _copy(CP_NEW)),
        # A first turn's: the thread has no checkpoint yet, and an export
        # renders its latest one.
        ("", None, _copy(CP_OLD)),
        (TASK, CP_OLD, _copy(CP_OLD)),
    ],
    ids=["another-live-save", "a-newer-render", "first-turn", "a-tasks-render"],
)
async def test_what_replaces_a_live_copy(thread, prefix, live, replacement):
    await _save(thread, _copy(live, live=True), prefix)

    assert await _save(thread, replacement, prefix)

    header, _ = await thread.read(prefix)
    assert header["fingerprint"] == replacement.fingerprint


# -- one agent's copy ----------------------------------------------------------


async def test_a_save_leaves_the_other_agents_alone(thread):
    await _save(thread, _copy(CP_NEW, inline={f"{TASK}run-0001.jsonl": b"task"}), TASK)
    await _save(thread, _copy(CP_OLD, inline={"turn-0001.jsonl": b"a"}))

    # Each agent is ordered on its own: the task's newer checkpoint does not
    # hold the thread's own copy back.
    assert await _save(thread, _copy(CP_OLD, inline={"turn-0002.jsonl": b"b"}))

    task, files = await thread.read(TASK)
    assert task["checkpoint_id"] == CP_NEW
    assert set(files) == {"turn-0002.jsonl", f"{TASK}run-0001.jsonl"}


async def test_a_file_resent_without_bytes_keeps_them_when_its_sha_is_unchanged(thread):
    await _save(thread, _copy(inline={"turn-0001.jsonl": b"kept"}))

    carried = {"turn-0001.jsonl": (_sha(b"kept"), 4)}
    assert await _save(thread, _copy(CP_NEW, files=carried))

    _, files = await thread.read()
    assert files["turn-0001.jsonl"] == (_sha(b"kept"), b"kept")


async def test_a_file_resent_without_bytes_under_a_new_sha_drops_the_old_bytes(thread):
    await _save(thread, _copy(inline={"turn-0001.jsonl": b"before"}))

    changed = {"turn-0001.jsonl": (_sha(b"after"), 5)}
    assert await _save(thread, _copy(CP_NEW, files=changed))

    _, files = await thread.read()
    assert files["turn-0001.jsonl"] == (_sha(b"after"), None)


async def test_files_no_longer_in_the_copy_are_deleted(thread):
    await _save(thread, _copy(inline={"turn-0001.jsonl": b"a", "turn-0002.jsonl": b"b"}))

    assert await _save(thread, _copy(CP_NEW, inline={"turn-0001.jsonl": b"a"}))

    _, files = await thread.read()
    assert set(files) == {"turn-0001.jsonl"}


async def test_a_save_for_a_deleted_thread_returns_false(thread, test_db_pool):
    async with test_db_pool.connection() as conn:
        await conn.execute(
            "DELETE FROM conversation_threads WHERE conversation_thread_id = %s",
            (thread.id,),
        )

    assert not await _save(thread, _copy(inline={"turn-0001.jsonl": b"late"}))
