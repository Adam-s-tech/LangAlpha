"""Stored digests of the tokens a computer's file mount presents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import FENCE_NOT_DELETED, advisory_key


@asynccontextmanager
async def publish_lock(computer_id: str) -> AsyncIterator[None]:
    """Hold one computer's mint-and-publish turn through the sandbox write.

    Two workers minting at once could each push the other's token out of
    both slots, and whichever write reached the sandbox last would hold a
    token that no longer authenticates.
    """
    async with get_db_connection() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s)", (advisory_key("LFS", computer_id),)
        )
        yield


async def save_token(
    computer_id: str, user_id: str, digest: bytes, expires_at: datetime
) -> None:
    """Make ``digest`` the current token; the one it replaces stays valid
    until its own expiry, so a daemon mid-request keeps working."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO livefs_tokens AS t
                (computer_id, user_id, token_sha256, expires_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (computer_id) DO UPDATE
                SET prev_token_sha256 = t.token_sha256,
                    prev_expires_at = t.expires_at,
                    token_sha256 = EXCLUDED.token_sha256,
                    expires_at = EXCLUDED.expires_at,
                    user_id = EXCLUDED.user_id,
                    updated_at = NOW()
            """,
            (computer_id, user_id, digest, expires_at),
        )


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


async def token_expiry(computer_id: str) -> datetime | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT expires_at FROM livefs_tokens WHERE computer_id = %s",
                (computer_id,),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def delete_token(computer_id: str) -> None:
    async with get_db_connection() as conn:
        await conn.execute(
            "DELETE FROM livefs_tokens WHERE computer_id = %s", (computer_id,)
        )
