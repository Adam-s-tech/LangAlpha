"""The endpoint a computer's file mount reads and saves the user's files through.

Authenticated by the computer's mount token alone, never the app's user
auth: the caller is a machine, and its token names the one user and computer
it acts for (``services/livefs/tokens.py``). Paths are mount-relative
(``user/memory/notes.md``); errors carry a ``code`` the daemon turns into an
errno and a ``message`` that reaches the agent through the tool result.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ptc_agent.core.sandbox.livefs_mount import CallContext
from ptc_agent.core.sandbox.livefs_runtime.protocol import (
    CALL_HEADER,
    MAX_FILE_BYTES,
    PREFIX,
    PROVISIONAL_HEADER,
)
from src.server.app import setup
from src.server.services.livefs import outcomes
from src.server.services.livefs.routes import LivefsError
from src.server.services.livefs.tokens import (
    LivefsAuthError,
    LivefsIdentity,
    LivefsThrottled,
    authenticate,
)
from src.server.services.livefs.tree import LivefsTree

logger = logging.getLogger(__name__)

router = APIRouter(prefix=PREFIX, tags=["Livefs"])

#: A change's outcome fields (None ones left out) and the response it answers with.
_Change = Callable[[LivefsTree], Awaitable[tuple[dict[str, Any], Any]]]


async def _caller(
    request: Request, authorization: str | None = Header(None)
) -> LivefsIdentity:
    try:
        return await authenticate(authorization)
    except LivefsAuthError:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired mount token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except LivefsThrottled as exc:
        # Refused before the request runs, so a retried save cannot land
        # twice. The program sees EAGAIN, which most never retry, so the
        # command's tool result says to.
        call_id = outcomes.valid_call_id(request.headers.get(CALL_HEADER))
        if call_id:
            await outcomes.throttled(exc.identity.computer_id, call_id)
        raise HTTPException(
            status_code=429,
            detail="Too many file requests from this computer",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def _error(exc: LivefsError) -> JSONResponse:
    return JSONResponse(
        {"code": exc.code, "message": exc.message, "path": exc.path},
        status_code=exc.status,
    )


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
            # The report is the user's automations by name; the tool result
            # carries it, the log does not.
            **{k: v for k, v in outcome.items() if k != "report"},
        },
    )
    await outcomes.record(identity.computer_id, call_id, outcome)


async def _saving_tree(identity: LivefsIdentity, call_id: str | None) -> LivefsTree:
    """The tree a change goes through, with who its command runs for. A
    change whose command filed nothing (Redis was down, or the filing
    expired) runs for no conversation or workspace, on the user's own clock."""
    context = await outcomes.call_context(identity.computer_id, call_id)
    return LivefsTree(identity, setup.store, context or CallContext())


async def _mutation(
    request: Request, identity: LivefsIdentity, op: str, change: _Change
) -> Any:
    """Make one change and file its outcome for the command that made it.

    A refused provisional save is only logged: the daemon retries it at
    release unless real bytes follow, and that retry is the command's.
    """
    call_id = outcomes.valid_call_id(request.headers.get(CALL_HEADER))
    try:
        fields, response = await change(await _saving_tree(identity, call_id))
    except LivefsError as exc:
        refused = {"op": op, "path": exc.path, "ok": False, "error": exc.message}
        if request.headers.get(PROVISIONAL_HEADER):
            logger.info("livefs provisional %s refused", op, extra=refused)
        else:
            await _report(identity, call_id, refused)
        return _error(exc)
    kept = {k: v for k, v in fields.items() if v is not None}
    await _report(identity, call_id, {"op": op, "ok": True, **kept})
    return response


async def _read_body(request: Request) -> bytes:
    """The body, stopping once it is past the cap, which the tree refuses."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FILE_BYTES:
            break
    return bytes(body)


@router.get("/list")
async def list_dir(
    path: str = Query(""),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    try:
        listing = await LivefsTree(identity, setup.store).list(path)
    except LivefsError as exc:
        return _error(exc)
    return listing._asdict()


@router.get("/read")
async def read_file(
    path: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Response:
    try:
        content, version, _ = await LivefsTree(identity, setup.store).read(path)
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
    async def save(tree: LivefsTree) -> tuple[dict[str, Any], Any]:
        saved = await tree.write(
            path,
            await _read_body(request),
            if_match=request.headers.get("if-match"),
            if_none_match=request.headers.get("if-none-match"),
        )
        return (
            {"path": saved.path, "size": saved.size, "report": saved.report},
            {"version": saved.version, "size": saved.size, "as_sent": saved.as_sent},
        )

    return await _mutation(request, identity, "write", save)


@router.post("/delete")
async def delete_file(
    request: Request,
    path: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    async def remove(tree: LivefsTree) -> tuple[dict[str, Any], Any]:
        return {"path": await tree.delete(path)}, Response(status_code=204)

    return await _mutation(request, identity, "delete", remove)


@router.post("/rename")
async def rename_file(
    request: Request,
    path: str = Query(...),
    to: str = Query(...),
    identity: LivefsIdentity = Depends(_caller),
) -> Any:
    async def move(tree: LivefsTree) -> tuple[dict[str, Any], Any]:
        source, target, report = await tree.rename(path, to)
        return {"path": target, "from": source, "report": report}, Response(status_code=204)

    return await _mutation(request, identity, "rename", move)
