"""Fill the transcript store for threads that were never stored, or are behind.

The file mount serves each thread's transcript from the store, and a computer
coming up renders every thread of its workspaces that the store is behind on.
Threads from before the store existed have no copy, so the first bring-up of
every computer would render all of them. This renders them ahead of time
instead, one thread at a time with a pause between, newest first. No sandbox
is touched, so no evicted result is marked missing. Safe to stop and rerun: a
thread already current is skipped.

Run inside the backend container (needs the app's env and venv); every render
reads a whole checkpoint into this process, so keep the concurrency low:

    /app/.venv/bin/python scripts/utils/backfill_transcripts.py            # count only
    /app/.venv/bin/python scripts/utils/backfill_transcripts.py --apply
    /app/.venv/bin/python scripts/utils/backfill_transcripts.py --apply --days 60
    /app/.venv/bin/python scripts/utils/backfill_transcripts.py --apply --thread <id>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

_BATCH = 200


async def _open_infra():
    from src.server.app import setup
    from src.server.database import pool as db_pool
    from src.server.utils.checkpointer import get_checkpointer, open_checkpointer_pool

    pool = db_pool.get_or_create_pool()
    await pool.open()
    checkpointer = get_checkpointer(
        "postgres",
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "postgres"),
        db_user=os.getenv("DB_USER", "postgres"),
        db_password=os.getenv("DB_PASSWORD", "postgres"),
    )
    await open_checkpointer_pool(checkpointer)
    setup.checkpointer = checkpointer


async def _threads(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """(thread id, workspace id, checkpoint id), most recently active first."""
    from src.server.database.pool import get_db_connection

    # Only workspaces on a computer: nothing else ever places a transcript.
    where = [
        "t.latest_checkpoint_id IS NOT NULL",
        "w.computer_id IS NOT NULL",
        "w.status <> 'deleted'",
    ]
    params: list = []
    if args.thread:
        where.append("t.conversation_thread_id = ANY(%s::uuid[])")
        params.append(args.thread)
    if args.workspace:
        where.append("t.workspace_id = %s::uuid")
        params.append(args.workspace)
    if args.user:
        where.append("w.user_id = %s")
        params.append(args.user)
    if args.days:
        where.append("t.updated_at >= NOW() - make_interval(days => %s)")
        params.append(args.days)
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "SELECT t.conversation_thread_id, t.workspace_id, t.latest_checkpoint_id "
            "FROM conversation_threads t "
            "JOIN workspaces w ON w.workspace_id = t.workspace_id "
            f"WHERE {' AND '.join(where)} ORDER BY t.updated_at DESC",
            params,
        )
        return [(str(r[0]), str(r[1]), r[2]) for r in await cur.fetchall()]


async def _stale(batch: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """(thread id, workspace id) of the page's threads the store is behind on."""
    from src.server.services.transcripts import behind_in_store

    behind = {b.thread_id for b in await behind_in_store([(t, c) for t, _, c in batch])}
    return [(t, w) for t, w, _ in batch if t in behind]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="store; without it, count only")
    parser.add_argument("--days", type=int, help="only threads active in the last N days")
    parser.add_argument("--user", help="only this user's threads")
    parser.add_argument("--workspace", help="only this workspace's threads")
    parser.add_argument("--thread", action="append", help="only these threads")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between threads")
    args = parser.parse_args()

    await _open_infra()
    from src.server.services.transcripts import export_thread

    threads = await _threads(args)
    if not args.apply:
        stale = 0
        for start in range(0, len(threads), _BATCH):
            stale += len(await _stale(threads[start : start + _BATCH]))
        print(f"{len(threads)} threads, {stale} not current in the store")
        return 0

    # A page at a time, each checked against the store as it comes up, so a
    # thread a turn end brought current meanwhile is skipped and at most
    # ``concurrency`` exports are ever in hand.
    done = stored = failed = 0
    began = time.monotonic()

    async def worker(queue: asyncio.Queue[tuple[str, str]]) -> None:
        nonlocal done, stored, failed
        while True:
            try:
                thread_id, workspace_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                counts = await export_thread(workspace_id, thread_id)
                stored += counts["stored"]
                failed += counts["failed"]
            except Exception as e:
                failed += 1
                print(f"{thread_id} failed: {e}")
            done += 1
            if done % 25 == 0:
                rate = done / max(time.monotonic() - began, 1e-6)
                print(f"{done} done, {stored} stored, {failed} failed, {rate:.1f}/s")
            await asyncio.sleep(args.pause)

    for start in range(0, len(threads), _BATCH):
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        for item in await _stale(threads[start : start + _BATCH]):
            queue.put_nowait(item)
        await asyncio.gather(*(worker(queue) for _ in range(max(1, args.concurrency))))
    print(f"{len(threads)} threads, {done} exported, {stored} stored, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
