"""The endpoint a computer's file mount reads and saves the user's files through.

Authenticated by the computer's mount token alone, never the app's user
auth: the caller is a machine, and its token names the one user and computer
it acts for (``services/livefs/tokens.py``). Paths are mount-relative
(``user/memory/notes.md``); errors carry a ``code`` the daemon turns into an
errno and a ``message`` that reaches the agent through the tool result.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ptc_agent.agent.backends.langgraph_store import MAX_CONTENT_BYTES
from ptc_agent.core.sandbox.livefs_runtime.protocol import PROVISIONAL_HEADER
from src.server.app import setup
from src.server.services.livefs import outcomes
from src.server.services.livefs.tokens import (
    LivefsAuthError,
    LivefsIdentity,
    authenticate,
)
from src.server.services.livefs.tree import LivefsError, LivefsTree
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/livefs", tags=["Livefs"])

# Per computer. A command that walks the mount costs one request per
# directory and file it touches, once per command, so this only binds on a
# runaway loop; it fails open when Redis is down.
_REQUESTS_PER_MINUTE = 1200

T = TypeVar("T")


async def _caller(authorization: str | None = Header(None)) -> LivefsIdentity:
    try:
        identity = await authenticate(authorization)
    except LivefsAuthError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired mount token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    await _rate_limit(identity.computer_id)
    return identity


async def _rate_limit(computer_id: str) -> None:
    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        return
    now = int(time.time())
    key = f"livefs:{{{computer_id}}}:rate:{now // 60}"
    try:
        async with cache.client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            count, _ = await pipe.execute()
    except Exception:
        return
    if count > _REQUESTS_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many file requests from this computer",
            headers={"Retry-After": str(60 - now % 60)},
        )


def _error(exc: LivefsError) -> JSONResponse:
    return JSONResponse(
        {"code": exc.code, "message": exc.message, "path": exc.path},
        status_code=exc.status,
    )


async def _answer(work: Awaitable[T], path: str) -> T:
    try:
        return await work
    except LivefsError:
        raise
    except Exception as exc:
        logger.exception("livefs request failed", extra={"path": path})
        raise LivefsError(
            503, "unavailable", f"{path} could not be reached; retry", path
        ) from exc


async def _report(
    identity: LivefsIdentity,
    call_id: str | None,
    outcome: dict[str, Any],
) -> None:
    logger.info(
        "livefs %s",
        outcome["op"],
        extra={
            "computer_id": identity.computer_id,
            "user_id": identity.user_id,
            "call_id": call_id,
            **outcome,
        },
    )
    await outcomes.record(identity.computer_id, call_id, outcome)


async def _read_body(request: Request, path: str) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_CONTENT_BYTES:
        raise _too_large(path)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CONTENT_BYTES:
            raise _too_large(path)
    return bytes(body)


def _too_large(path: str) -> LivefsError:
    return LivefsError(
        413,
        "too_large",
        f"{path}: files here hold at most {MAX_CONTENT_BYTES} bytes; split the content.",
        path,
    )


@router.get("/list")
async def list_dir(
    path: str = Query(""),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    try:
        entries, writable = await _answer(
            LivefsTree(identity, setup.store).list(path), path
        )
    except LivefsError as exc:
        return _error(exc)
    return {"entries": entries, "writable": writable}


@router.get("/read")
async def read_file(
    path: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Response:
    try:
        content, version, _ = await _answer(
            LivefsTree(identity, setup.store).read(path), path
        )
    except LivefsError as exc:
        return _error(exc)
    return Response(
        content.encode(),
        media_type="application/octet-stream",
        headers={"ETag": f'"{version}"'},
    )


@router.put("/write")
async def write_file(
    request: Request,
    path: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    call_id = outcomes.valid_call_id(request.headers.get("x-livefs-call"))
    try:
        body = await _read_body(request, path)
        version, size, saved = await _answer(
            LivefsTree(identity, setup.store).write(
                path,
                body,
                if_match=request.headers.get("if-match"),
                if_none_match=request.headers.get("if-none-match"),
            ),
            path,
        )
    except LivefsError as exc:
        outcome = {
            "op": "write", "path": exc.path or path, "ok": False, "error": exc.message
        }
        if request.headers.get(PROVISIONAL_HEADER):
            # The daemon retries it at release unless real bytes follow.
            logger.info("livefs provisional write refused", extra=outcome)
        else:
            await _report(identity, call_id, outcome)
        return _error(exc)
    await _report(
        identity, call_id, {"op": "write", "path": saved, "ok": True, "size": size}
    )
    return {"version": version, "size": size}


@router.post("/delete")
async def delete_file(
    request: Request,
    path: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    call_id = outcomes.valid_call_id(request.headers.get("x-livefs-call"))
    try:
        deleted = await _answer(LivefsTree(identity, setup.store).delete(path), path)
    except LivefsError as exc:
        await _report(
            identity,
            call_id,
            {"op": "delete", "path": exc.path or path, "ok": False, "error": exc.message},
        )
        return _error(exc)
    await _report(identity, call_id, {"op": "delete", "path": deleted, "ok": True})
    return Response(status_code=204)


@router.post("/rename")
async def rename_file(
    request: Request,
    path: str = Query(...),
    to: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    call_id = outcomes.valid_call_id(request.headers.get("x-livefs-call"))
    try:
        source, target = await _answer(
            LivefsTree(identity, setup.store).rename(path, to), path
        )
    except LivefsError as exc:
        await _report(
            identity,
            call_id,
            {"op": "rename", "path": exc.path or path, "ok": False, "error": exc.message},
        )
        return _error(exc)
    await _report(
        identity,
        call_id,
        {"op": "rename", "path": target, "from": source, "ok": True},
    )
    return Response(status_code=204)
