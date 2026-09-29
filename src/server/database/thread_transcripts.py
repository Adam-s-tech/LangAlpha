"""Stored thread transcripts, which the file mount serves.

Each agent of a thread (its own, and each background task's) is stored on its
own: a header row carries the manifest its copy was rendered with, and a file
row each other file, by its whole path in the thread's directory: its bytes
when they are small or there is no object storage, else the blob behind them.
A save replaces one agent's copy and leaves the others alone.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from psycopg.errors import ForeignKeyViolation
from psycopg.rows import dict_row

from ptc_agent.core.paths import THREAD_DIR_NAME
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


@dataclass
class StoredTranscript:
    """One agent's copy. A load fills the header alone; a save lists every
    file with what it has in hand for each."""

    fingerprint: str
    checkpoint_id: str | None
    manifest: str
    # Transcript-dir-relative path -> (sha256, byte_len).
    files: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: Bytes to keep in the row, by path.
    inline: dict[str, bytes] = field(default_factory=dict)
    #: Paths carried over unrendered from the copy this one replaces. Nothing
    #: here holds their bytes, so their rows must already hold this sha.
    carried: set[str] = field(default_factory=set)


class StaleCopy(Exception):
    """A save carried a file over that its row no longer matches: the stored
    copy moved after this one was rendered against it. Render it in full."""


@dataclass(frozen=True)
class StoredFile:
    #: The blob's namespace; None for a manifest, which is always in its row.
    user_id: str | None
    sha256: str
    content: bytes | None


async def stored_fingerprints(thread_ids: list[str]) -> dict[str, dict[str, str]]:
    """Each thread's stored agents: fingerprint by prefix."""
    if not thread_ids:
        return {}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT conversation_thread_id, prefix, fingerprint "
                "FROM thread_transcripts "
                "WHERE conversation_thread_id = ANY(%s::uuid[])",
                (thread_ids,),
            )
            found: dict[str, dict[str, str]] = {}
            for thread_id, prefix, fingerprint in await cur.fetchall():
                found.setdefault(str(thread_id), {})[prefix] = fingerprint
            return found


async def load_stored(
    thread_ids: list[str], prefix: str | None = None
) -> dict[str, dict[str, StoredTranscript]]:
    """Each thread's stored agents by prefix, or only the one ``prefix``
    names: the header alone, which is all a render needs of what it replaces."""
    if not thread_ids:
        return {}
    only = "" if prefix is None else " AND prefix = %s"
    params = (thread_ids,) if prefix is None else (thread_ids, prefix)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT conversation_thread_id, prefix, fingerprint, checkpoint_id, "
                "manifest FROM thread_transcripts "
                f"WHERE conversation_thread_id = ANY(%s::uuid[]){only}",
                params,
            )
            stored: dict[str, dict[str, StoredTranscript]] = {}
            for thread_id, agent, fingerprint, checkpoint_id, manifest in (
                await cur.fetchall()
            ):
                stored.setdefault(str(thread_id), {})[agent] = StoredTranscript(
                    fingerprint, checkpoint_id, manifest
                )
    return stored


async def workspace_checkpoints(workspace_id: str) -> list[tuple[str, str]]:
    """(thread id, checkpoint id) of each of the workspace's threads that has
    finished a turn, newest first: what its transcripts are rendered from."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT conversation_thread_id, latest_checkpoint_id
                FROM conversation_threads
                WHERE workspace_id = %s AND latest_checkpoint_id IS NOT NULL
                ORDER BY updated_at DESC
                """,
                (workspace_id,),
            )
            return [(str(row[0]), row[1]) for row in await cur.fetchall()]


async def workspace_transcripts(workspace_id: str) -> list[str]:
    """Ids of the workspace's threads that have a stored transcript."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT t.conversation_thread_id FROM conversation_threads t
                WHERE t.workspace_id = %s
                  AND EXISTS (
                      SELECT 1 FROM thread_transcripts s
                      WHERE s.conversation_thread_id = t.conversation_thread_id
                  )
                """,
                (workspace_id,),
            )
            return [str(row[0]) for row in await cur.fetchall()]


# The thread whose transcript directory is a short id: the newest one if two
# of the workspace's threads share the prefix. The prefix is a uuid range, so
# the primary key finds it.
_THREAD_BY_SHORT_ID = """
    WITH thread AS (
        SELECT t.conversation_thread_id AS id FROM conversation_threads t
        WHERE t.workspace_id = %s
          AND t.conversation_thread_id BETWEEN %s::uuid AND %s::uuid
          AND EXISTS (
              SELECT 1 FROM thread_transcripts s
              WHERE s.conversation_thread_id = t.conversation_thread_id
          )
        ORDER BY t.updated_at DESC
        LIMIT 1
    )
"""


def _short_id_range(short_id: str) -> tuple[str, str] | None:
    if not THREAD_DIR_NAME.match(short_id):
        return None
    return (
        f"{short_id}-0000-0000-0000-000000000000",
        f"{short_id}-ffff-ffff-ffff-ffffffffffff",
    )


async def list_transcript(
    workspace_id: str, short_id: str
) -> list[tuple[str, str | None, str, int]] | None:
    """Every file of the transcript directory ``short_id`` names, as (agent
    prefix, path, sha256, size), an agent's manifest with the path None;
    None when there is no such directory."""
    bounds = _short_id_range(short_id)
    if bounds is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _THREAD_BY_SHORT_ID
                + """
                SELECT s.prefix, NULL, s.manifest_sha256, s.manifest_len
                FROM thread_transcripts s JOIN thread ON s.conversation_thread_id = thread.id
                UNION ALL
                SELECT f.prefix, f.path, f.sha256, f.byte_len
                FROM thread_transcript_files f
                JOIN thread ON f.conversation_thread_id = thread.id
                """,
                (workspace_id, *bounds),
            )
            rows = await cur.fetchall()
    return [
        (prefix, path, sha256, int(byte_len)) for prefix, path, sha256, byte_len in rows
    ] or None


async def load_transcript_file(
    workspace_id: str, short_id: str, path: str, *, manifest_of: str | None = None
) -> StoredFile | None:
    """One file of the transcript directory ``short_id`` names, or the
    manifest of the agent ``manifest_of`` names, in one round trip."""
    bounds = _short_id_range(short_id)
    if bounds is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if manifest_of is not None:
                await cur.execute(
                    _THREAD_BY_SHORT_ID
                    + """
                    SELECT s.manifest_sha256, s.manifest FROM thread_transcripts s
                    JOIN thread ON s.conversation_thread_id = thread.id
                    WHERE s.prefix = %s
                    """,
                    (workspace_id, *bounds, manifest_of),
                )
                row = await cur.fetchone()
                return StoredFile(None, row[0], row[1].encode()) if row else None
            await cur.execute(
                _THREAD_BY_SHORT_ID
                + """
                SELECT f.user_id, f.sha256, f.content FROM thread_transcript_files f
                JOIN thread ON f.conversation_thread_id = thread.id
                WHERE f.path = %s
                """,
                (workspace_id, *bounds, path),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return StoredFile(row[0], row[1], bytes(row[2]) if row[2] is not None else None)


def _changed_rows(
    thread_id: str,
    prefix: str,
    user_id: str,
    transcript: StoredTranscript,
    held: dict[str, tuple[str, str, bool]],
) -> list[tuple]:
    """The file rows a save has to write: new, moved to another sha, or
    gaining bytes a blob-backed row lacked. ``held`` is what the rows hold,
    (sha256, user_id, has bytes) by path."""
    rows = []
    for path, (sha256, byte_len) in transcript.files.items():
        data = transcript.inline.get(path)
        row = held.get(path)
        if row is not None and row[:2] == (sha256, user_id) and (row[2] or data is None):
            continue
        if path in transcript.carried:
            raise StaleCopy(path)
        rows.append((thread_id, prefix, path, user_id, sha256, byte_len, data))
    return rows


async def save_stored(
    thread_id: str, prefix: str, user_id: str, transcript: StoredTranscript
) -> bool:
    """Replace one agent's stored copy unless a newer render already landed.

    Checkpoint ids sort by time, so a save racing a later one (another
    worker's turn end) loses on the header row's lock and leaves the newer
    copy alone. Only the file rows that differ are written, read under that
    lock. A file row without bytes names a blob, which must be registered.
    Returns whether this save landed; raises ``StaleCopy`` when a file it
    carried over no longer matches its row.
    """
    manifest = transcript.manifest.encode()
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        INSERT INTO thread_transcripts AS s
                            (conversation_thread_id, prefix, fingerprint,
                             checkpoint_id, manifest, manifest_sha256,
                             manifest_len, stored_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                        ON CONFLICT (conversation_thread_id, prefix) DO UPDATE
                            SET fingerprint = EXCLUDED.fingerprint,
                                checkpoint_id = EXCLUDED.checkpoint_id,
                                manifest = EXCLUDED.manifest,
                                manifest_sha256 = EXCLUDED.manifest_sha256,
                                manifest_len = EXCLUDED.manifest_len,
                                stored_at = clock_timestamp()
                            WHERE s.checkpoint_id IS NULL
                               OR EXCLUDED.checkpoint_id IS NULL
                               OR s.checkpoint_id <= EXCLUDED.checkpoint_id
                        RETURNING 1
                        """,
                        (
                            thread_id,
                            prefix,
                            transcript.fingerprint,
                            transcript.checkpoint_id,
                            transcript.manifest,
                            hashlib.sha256(manifest).hexdigest(),
                            len(manifest),
                        ),
                    )
                    if await cur.fetchone() is None:
                        return False
                    # Every save of this agent takes the header row first, so
                    # the rows read here are the ones this save replaces.
                    await cur.execute(
                        "SELECT path, sha256, user_id, content IS NOT NULL AS has_bytes "
                        "FROM thread_transcript_files "
                        "WHERE conversation_thread_id = %s AND prefix = %s",
                        (thread_id, prefix),
                    )
                    held = {
                        r["path"]: (r["sha256"], r["user_id"], r["has_bytes"])
                        for r in await cur.fetchall()
                    }
                    rows = _changed_rows(thread_id, prefix, user_id, transcript, held)
                    gone = [path for path in held if path not in transcript.files]
                    if gone:
                        await cur.execute(
                            "DELETE FROM thread_transcript_files "
                            "WHERE conversation_thread_id = %s AND path = ANY(%s)",
                            (thread_id, gone),
                        )
                    if rows:
                        await cur.executemany(
                            """
                            INSERT INTO thread_transcript_files AS f
                                (conversation_thread_id, prefix, path, user_id,
                                 sha256, byte_len, content)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (conversation_thread_id, path) DO UPDATE
                                SET user_id = EXCLUDED.user_id,
                                    sha256 = EXCLUDED.sha256,
                                    byte_len = EXCLUDED.byte_len,
                                    content = EXCLUDED.content
                            """,
                            rows,
                        )
    except ForeignKeyViolation:
        # The thread was deleted while its render was in flight.
        return False
    return True


async def delete_stored(thread_id: str, prefixes: set[str]) -> None:
    """Drop these agents' copies, files and all."""
    async with get_db_connection() as conn:
        await conn.execute(
            "DELETE FROM thread_transcripts "
            "WHERE conversation_thread_id = %s AND prefix = ANY(%s)",
            (thread_id, list(prefixes)),
        )
