"""Stored digests of the tokens a computer's file mount presents."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import FENCE_BINDABLE, FENCE_NOT_DELETED


async def save_token(
    computer_id: str, user_id: str, digest: bytes, expires_at: datetime
) -> bool:
    """Make ``digest`` the current token, held by no sandbox yet; the one it
    replaces stays valid until its own expiry, so a daemon mid-request keeps
    working.

    False when the computer is not its user's or is leaving service. The
    share lock on the computer orders the save against a stop: one saved
    first is revoked after it, and one waiting on it finds the computer
    stopping.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                WITH serving AS (
                    SELECT computer_id, user_id FROM computers
                    WHERE computer_id = %s AND user_id = %s AND {FENCE_BINDABLE}
                    FOR SHARE
                )
                INSERT INTO livefs_tokens AS t
                    (computer_id, user_id, token_sha256, expires_at, updated_at)
                SELECT computer_id, user_id, %s, %s, NOW() FROM serving
                ON CONFLICT (computer_id) DO UPDATE
                    SET prev_token_sha256 = t.token_sha256,
                        prev_expires_at = t.expires_at,
                        token_sha256 = EXCLUDED.token_sha256,
                        expires_at = EXCLUDED.expires_at,
                        held_by = NULL,
                        user_id = EXCLUDED.user_id,
                        updated_at = NOW()
                RETURNING computer_id
                """,
                (computer_id, user_id, digest, expires_at),
            )
            return await cur.fetchone() is not None


async def load_token(computer_id: str) -> dict[str, Any] | None:
    """The computer's token digests, with the owner and root it serves.

    Joined to the computer so a deleted one, or one that changed hands,
    authenticates nothing.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT t.user_id, t.token_sha256, t.expires_at,
                       t.prev_token_sha256, t.prev_expires_at, c.root_dir
                FROM livefs_tokens t
                JOIN computers c ON c.computer_id = t.computer_id
                WHERE t.computer_id = %s AND c.user_id = t.user_id
                  AND c.{FENCE_NOT_DELETED}
                """,
                (computer_id,),
            )
            return await cur.fetchone()


async def current_token(computer_id: str) -> tuple[datetime | None, str | None]:
    """When the current token runs out, and the sandbox that took it: None
    until a publish lands, since one can fail after its mint."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT expires_at, held_by FROM livefs_tokens WHERE computer_id = %s",
                (computer_id,),
            )
            row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def mark_held(computer_id: str, sandbox_id: str | None, expires_at: datetime) -> None:
    """Record that ``sandbox_id`` took the token expiring then, and only
    that one: a token minted since is held by no sandbox yet."""
    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE livefs_tokens SET held_by = %s"
            " WHERE computer_id = %s AND expires_at = %s",
            (sandbox_id, computer_id, expires_at),
        )


async def delete_token(computer_id: str) -> None:
    async with get_db_connection() as conn:
        await conn.execute(
            "DELETE FROM livefs_tokens WHERE computer_id = %s", (computer_id,)
        )
