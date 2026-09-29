"""What code saved through the mount, for the tool call that ran it.

A save through the mount fails at close() with nothing but an errno, so the
reason reaches the agent in the result of the tool call that ran the code:
the daemon tags each request with the call id of the process making it, the
server files the outcome under that id, and the tool collects the list when
its command returns. A process still saving after that, such as a background
job, files under the computer's late list, which the next collect picks up.

Redis only: an outcome is a report, not state, and losing one to a Redis
outage costs a line in a tool result, never a save.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

_CALL_TTL_S = 3600
_LATE_TTL_S = 86400
_LATE_CAP = 50
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Filed under the call unless it was already collected, in which case the
# save came after its command returned and belongs to the late list. One
# script, so a collect can never land between the check and the push.
_RECORD = """
if redis.call('EXISTS', KEYS[2]) == 0 then
  redis.call('RPUSH', KEYS[1], ARGV[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
redis.call('RPUSH', KEYS[3], ARGV[1])
redis.call('LTRIM', KEYS[3], -tonumber(ARGV[4]), -1)
redis.call('EXPIRE', KEYS[3], ARGV[3])
return 0
"""


def valid_call_id(call_id: str | None) -> str | None:
    return call_id if call_id and _CALL_ID_RE.fullmatch(call_id) else None


def _keys(computer_id: str, call_id: str) -> tuple[str, str, str]:
    tag = f"livefs:{{{computer_id}}}"
    return f"{tag}:out:{call_id}", f"{tag}:done:{call_id}", f"{tag}:late"


async def record(computer_id: str, call_id: str | None, outcome: dict[str, Any]) -> None:
    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        return
    payload = json.dumps(outcome)
    out_key, done_key, late_key = _keys(computer_id, call_id or "-")
    try:
        if call_id:
            await cache.client.eval(
                _RECORD, 3, out_key, done_key, late_key,
                payload, _CALL_TTL_S, _LATE_TTL_S, _LATE_CAP,
            )
            return
        async with cache.client.pipeline(transaction=True) as pipe:
            pipe.rpush(late_key, payload)
            pipe.ltrim(late_key, -_LATE_CAP, -1)
            pipe.expire(late_key, _LATE_TTL_S)
            await pipe.execute()
    except Exception:
        logger.warning("livefs outcome not recorded", exc_info=True)


def _decode(raw: list[Any], *, late: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw or ():
        try:
            outcome = json.loads(item)
        except (TypeError, ValueError):
            continue
        if late:
            outcome["late"] = True
        out.append(outcome)
    return out


async def collect(computer_id: str, call_id: str) -> list[dict[str, Any]]:
    """Close the call to further outcomes and return its own plus the late ones."""
    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        return []
    out_key, done_key, late_key = _keys(computer_id, call_id)
    try:
        async with cache.client.pipeline(transaction=True) as pipe:
            pipe.set(done_key, 1, ex=_CALL_TTL_S)
            pipe.lrange(out_key, 0, -1)
            pipe.delete(out_key)
            pipe.lrange(late_key, 0, -1)
            pipe.delete(late_key)
            _, own, _, late, _ = await pipe.execute()
    except Exception:
        logger.warning("livefs outcomes not collected", exc_info=True)
        return []
    return _decode(late, late=True) + _decode(own, late=False)


_REPORT_CAP = 10


def _sandbox_path(path: str, links: tuple[tuple[str, str], ...]) -> str:
    """The path the agent used, from the mount path the server filed."""
    for source, target in sorted(links, key=lambda link: len(link[0]), reverse=True):
        if path == source or path.startswith(source + "/"):
            return target + path[len(source):]
    return path


def describe(outcomes: list[dict[str, Any]], links: tuple[tuple[str, str], ...]) -> str:
    """The failed saves, for the tool result: a program writing through the
    mount learns of a refusal only at close(), which most never check."""
    failed = [o for o in outcomes if not o.get("ok")]
    if not failed:
        return ""
    lines = [
        f"- {_sandbox_path(str(o.get('path', '')), links)}: {o.get('error') or 'failed'}"
        + (" (from an earlier command)" if o.get("late") else "")
        for o in failed[:_REPORT_CAP]
    ]
    if len(failed) > _REPORT_CAP:
        lines.append(f"- and {len(failed) - _REPORT_CAP} more")
    return "NOT SAVED (the exit status does not show this):\n" + "\n".join(lines)
