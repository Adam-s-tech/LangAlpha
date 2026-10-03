"""
Public Share Router — Unauthenticated endpoints for shared thread access.

All endpoints use an opaque share_token instead of thread/workspace IDs.
No auth required. workspace_id is resolved server-side and never exposed.
A token resolves to a (workspace, path) scope; the file endpoints serve that
path and its subtree and nothing else, whatever path the URL asks for.

The metadata route also answers for a file or app share link, which is
what the ``/a/`` page dispatches on. It takes optional auth for that: a
private link opens for its signed-in owner and for nobody else.

This module carries the thread itself: the metadata a viewer opens and the SSE
replay, with the owner-only marks stripped out of every event by
``services/share_redaction``. What the token authorizes lives in
``share_access``, the file routes in ``share_files``, and the branded failure
page in ``share_pages``.

Endpoints:
- GET /api/v1/public/shared/{share_token}          - Thread, file or app metadata
- GET /api/v1/public/shared/{share_token}/replay    — SSE conversation replay
- GET /api/v1/public/shared/{share_token}/files     — File listing (requires allow_files)
- GET /api/v1/public/shared/{share_token}/files/read     — Read file content (requires allow_files)
- GET /api/v1/public/shared/{share_token}/files/serve/{path} — Serve file inline with sandboxed CSP (requires allow_files)
- GET /api/v1/public/shared/{share_token}/files/download — Download raw file (requires allow_download)
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from src.observability import observe_replay_stream

from src.server.app.share_access import (
    LinkAccess,
    get_permissions,
    get_shared_thread,
    resolve_link,
)
from src.server.app.share_files import share_files_router
from src.server.app.workspace_sandbox import (
    owner_preview_url,
    signed_url_expires_at,
    with_preview_path,
)
from src.server.database.conversation import (
    get_queries_for_thread,
    get_responses_for_thread,
)
from src.server.database.share_links import KIND_APP
from src.server.services.file_grants import grant_prefix, mint_file_grant, seconds_left
from src.server.utils.api import PageViewer, Viewer
from src.server.services.history.replay.items import run_completed_at
from src.server.services.history.replay.stopped import stop_close_item
from src.server.services.share_redaction import ShareRedaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["Public Sharing"])
# The file routes live next door and mount here, so the token prefix and the
# tag stay in one place.
router.include_router(share_files_router)

# =============================================================================
# METADATA
# =============================================================================


async def _link_metadata(
    access: LinkAccess, user_id: str | None, path: str | None
) -> dict[str, Any]:
    """What the ``/a/`` page renders for a link ``resolve_link`` admitted.

    ``expires_in`` is how many seconds the credential in ``url`` or
    ``frame_base`` has left, so the page renews it just before rather than on a
    timer. A public file
    has none: its route re-checks the link on every request.
    """
    link = access.link
    if link.kind == KIND_APP:
        # An app link is never shared, so only its owner is ever here.
        url = await owner_preview_url(link.workspace_id, user_id, link.port)
        return {
            "kind": "app",
            "title": link.display_title,
            "url": with_preview_path(url, path or link.path),
            "expires_in": seconds_left(await signed_url_expires_at(url)),
        }

    file = {"kind": "file", "name": link.display_title, "path": link.path}
    if access.owner:
        grant = await mint_file_grant(link.workspace_id)
        return {
            **file,
            "access": "owner",
            "frame_base": grant_prefix(grant),
            "expires_in": seconds_left(grant.expires_at),
        }
    return {
        **file,
        "access": "public",
        "frame_base": f"/api/v1/public/shared/{link.code}/files/serve/",
    }


@router.get("/shared/{share_token}")
async def get_shared_thread_metadata(
    share_token: str,
    page_viewer: PageViewer,
    response: Response,
    view_as: str | None = Query(None, alias="as"),
    path: str | None = Query(None),
):
    """Metadata for a shared thread, file or app. Auth is optional.

    ``?as=visitor`` drops the viewer, so the owner sees exactly what a visitor
    sees, a private link included. ``?path=`` opens an app at a page other
    than its entry, which is how an old preview URL keeps its suffix. A token
    that is not a link is a thread token and answers as it always has.
    """
    # The answer depends on the bearer and can carry the owner's grant, so no
    # cache between here and the browser may hand it to the next visitor.
    response.headers["Cache-Control"] = "no-store"
    viewer = Viewer(None) if view_as == "visitor" else page_viewer
    try:
        access = await resolve_link(share_token, user_id=viewer.user_id)
        thread = None if access is not None else await get_shared_thread(share_token)
    except HTTPException as e:
        # Not found is said only to a viewer the keys could check. Any other
        # may be the owner, and a private link and an unknown code must still
        # answer alike, so both wait for the keys.
        if e.status_code == 404 and viewer.unconfirmed is not None:
            raise viewer.unconfirmed from None
        raise
    if access is not None:
        return await _link_metadata(access, viewer.user_id, path)

    perms = get_permissions(thread)

    return {
        "kind": "thread",
        "thread_id": str(thread["conversation_thread_id"]),
        "title": thread.get("title"),
        "msg_type": thread.get("msg_type"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "workspace_name": thread.get("workspace_name"),
        "permissions": {
            "allow_files": perms.get("allow_files", False),
            "allow_download": perms.get("allow_download", False),
        },
    }


# =============================================================================
# REPLAY
# =============================================================================


@router.get("/shared/{share_token}/replay")
async def replay_shared_thread(share_token: str):
    """Replay a shared thread as SSE. No auth required.

    Same replay logic as the authenticated endpoint, but resolves
    thread via share_token and strips sensitive fields.
    """
    thread = await get_shared_thread(share_token)
    thread_id = str(thread["conversation_thread_id"])

    queries, _ = await get_queries_for_thread(thread_id)
    responses, _ = await get_responses_for_thread(thread_id)
    responses_by_turn = {r.get("turn_index"): r for r in responses if isinstance(r, dict)}

    # Public replay has no /status reconciliation at all, so without the
    # stamp its task cards would be stuck "running" forever (see
    # history/task_status.py). Only the whitelisted status value is added.
    from src.server.services.history.task_status import (
        collect_task_ids,
        resolve_task_details,
        stamp_task_artifact_data,
    )

    stored_events = [
        item
        for r in responses_by_turn.values()
        if isinstance(r.get("sse_events"), list)
        for item in r["sse_events"]
        if isinstance(item, dict)
    ]
    redaction = ShareRedaction(stored_events)

    task_details: dict[str, dict] = {}
    try:
        task_details = await resolve_task_details(
            thread_id, collect_task_ids(stored_events)
        )
    except Exception:
        logger.warning(
            f"[PUBLIC REPLAY] task-status stamping failed for {thread_id}",
            exc_info=True,
        )

    async def event_generator():
        seq = 0

        for q in queries:
            if not isinstance(q, dict):
                continue

            turn_index = q.get("turn_index")
            seq += 1

            content, metadata = redaction.query(q)
            payload = {
                "thread_id": thread_id,
                "turn_index": turn_index,
                "content": content,
                "timestamp": q.get("created_at"),
                "metadata": metadata,
            }
            # Tag system queries so the frontend can hide the user bubble
            query_type = q.get("type")
            if query_type == "system":
                payload["query_type"] = "system"
            # The turn's end, paired with the query timestamp above to give the
            # fold row its duration. This payload is hand-built rather than
            # taken from the replay builder, so the field has to be mirrored
            # here or a shared transcript folds with no duration to show. The
            # run id the builder also stamps stays out: it exists for the
            # report-back catch-up, which a public viewer never runs.
            completed_at = run_completed_at(responses_by_turn.get(turn_index))
            if completed_at is not None:
                payload["run_completed_at"] = completed_at

            yield (
                f"id: {seq}\n"
                f"event: user_message\n"
                f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            )

            response = responses_by_turn.get(turn_index)
            if not response:
                continue

            sse_events = response.get("sse_events")
            if not isinstance(sse_events, list):
                sse_events = []
            # A stop during bring-up can archive nothing and still owes its close.
            stop_close = stop_close_item(thread_id, response, sse_events)
            if stop_close:
                sse_events = [*sse_events, stop_close]

            for item in sse_events:
                if not isinstance(item, dict):
                    continue
                event_type = item.get("event")
                data = item.get("data")
                if not event_type or not isinstance(data, dict):
                    continue
                replay_data = redaction.event(event_type, data)
                if replay_data is None:
                    continue

                seq += 1
                replay_data.setdefault("thread_id", thread_id)
                replay_data["turn_index"] = turn_index
                replay_data["response_id"] = str(response.get("conversation_response_id"))
                if task_details:
                    replay_data = stamp_task_artifact_data(
                        replay_data, task_details, status_only=True
                    )

                yield (
                    f"id: {seq}\n"
                    f"event: {event_type}\n"
                    f"data: {json.dumps(replay_data, ensure_ascii=False, default=str)}\n\n"
                )

        seq += 1
        yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps({'thread_id': thread_id}, default=str)}\n\n"

    return StreamingResponse(
        observe_replay_stream(event_generator(), source="public"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
