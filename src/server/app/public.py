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
replay, with the owner-only marks stripped out of every event. What the token
authorizes lives in ``share_access``, the file routes in ``share_files``, and
the branded failure page in ``share_pages``.

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

from ptc_agent.agent.middleware.direct_mcp import METADATA_KEY
from ptc_agent.agent.middleware.order_governance import RECEIPT_KEY
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
from src.tools.secretary import SECRETARY_TOOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["Public Sharing"])
# The file routes live next door and mount here, so the token prefix and the
# tag stay in one place.
router.include_router(share_files_router)

# A shared thread is readable by anyone holding the link, and five things a turn
# carries name the owner's brokerage account: the provenance record of a direct
# tool call, an order call's own arguments, the order receipt stamped on that
# call's artifact, the vendor's own answer to an order call, and the verdict the
# owner's resume recorded against the order it answered. None is rendered for a
# viewer, so none is sent. The owner-only rule cannot live in the client once
# the payload has already left the server.
#
# ``tool_call_chunks`` go whole: they are the arguments again, streamed in
# pieces that often carry no call id to match, and no replay reads them.
_DROPPED_EVENTS = frozenset({"provenance", "tool_call_chunks"})
_PRIVATE_ARTIFACT_KEYS = (RECEIPT_KEY, "provenance")
# The owner's own turn, and the resume that answers an approval names every
# order it decided by the attempt's ledger id. A viewer cannot answer one and
# must not read which of the owner's orders were approved.
_PRIVATE_QUERY_METADATA_KEYS = frozenset({"workspace_id", "order_decisions"})
# Keys naming the owner's account wherever they sit in an event: no share
# renders them, and none is the viewer's to read.
_OWNER_ID_KEYS = frozenset({"workspace_id", "user_id"})
# The one mark a direct MCP call carries before its answer: ``direct_tool_name``
# builds every direct tool name under this prefix, its digest forms included.
_DIRECT_TOOL_PREFIX = "mcp__"
# The flash agent's account tools list, create, delete and dispatch into the
# owner's workspaces and threads. Their answers are the owner's own rows as
# text, sandbox and user ids included, which no key strip reaches; their
# arguments and approvals name the same things. A share renders none of these
# calls, so each travels as its name and id alone.
_SECRETARY_TOOL_NAMES = frozenset(t.name for t in SECRETARY_TOOLS)


def _is_order_result(data: dict[str, Any]) -> bool:
    """Whether a tool result answers an order call.

    The stamp marks the call, not the receipt: a ledger write that failed
    leaves no receipt and the same answer.
    """
    artifact = data.get("artifact")
    if not isinstance(artifact, dict):
        return False
    stamp = artifact.get(METADATA_KEY)
    return (
        isinstance(stamp, dict) and isinstance(stamp.get("order"), dict)
    ) or RECEIPT_KEY in artifact


def _order_call_ids(events: list[dict[str, Any]]) -> set[str]:
    """The id of every order call in a thread, read before any event is sent.

    A call is streamed before anything names it an order, and an approval ends
    its turn, so the result that names it can sit in a later turn than the call.
    """
    found: list[Any] = []
    for item in events:
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if item.get("event") == "tool_call_result" and _is_order_result(data):
            found.append(data.get("tool_call_id"))
        elif item.get("event") == "interrupt" and isinstance(
            data.get("action_requests"), list
        ):
            found.extend(
                r.get("tool_call_id")
                for r in data["action_requests"]
                if isinstance(r, dict) and "attempt_id" in r
            )
    return {str(i) for i in found if i}


def _cleared_call_ids(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Every call, as message id and call id, whose answer shows it was not an order.

    Only a direct tool's answer carries the binder's stamp, so it is the one
    proof a direct call never touched an order. An answer names only its call
    id, which a provider may repeat in a later message, so it clears the last
    message before it to make that call, however many turns back: only the main
    agent holds direct tools, and it is not asked again until a message's calls
    are answered.
    """
    made_by: dict[str, Any] = {}
    found: set[tuple[str, str]] = set()
    for item in events:
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if item.get("event") == "tool_calls" and isinstance(
            data.get("tool_calls"), list
        ):
            for call in data["tool_calls"]:
                if isinstance(call, dict) and call.get("id"):
                    made_by[str(call["id"])] = data.get("id")
            continue
        if item.get("event") != "tool_call_result":
            continue
        artifact = data.get("artifact")
        if not isinstance(artifact, dict) or RECEIPT_KEY in artifact:
            continue
        stamp = artifact.get(METADATA_KEY)
        call_id = data.get("tool_call_id")
        message_id = made_by.get(str(call_id))
        # The binder stamps ``order`` null on a call that does nothing to one.
        # The stream writes "unknown" for a message that came with no id.
        if (
            call_id
            and message_id not in (None, "", "unknown")
            and isinstance(stamp, dict)
            and stamp.get("order") is None
        ):
            found.add((str(message_id), str(call_id)))
    return found


def _may_be_order(
    call: dict[str, Any],
    message_id: Any,
    order_calls: set[str],
    cleared_calls: set[tuple[str, str]],
) -> bool:
    """Whether a call's arguments may name an order, and so stay off a shared thread.

    Fails closed on a direct call: an order stopped, or lost with its worker,
    before the vendor answered leaves no result and no approval card to mark
    it. So a direct call keeps its arguments only once an answer clears it, and
    a stopped one that was not an order shows none either.
    """
    if call.get("id") in order_calls:
        return True
    name = call.get("name")
    return (
        isinstance(name, str)
        and name.startswith(_DIRECT_TOOL_PREFIX)
        and (message_id, call.get("id")) not in cleared_calls
    )


def _strip_call_args(
    data: dict[str, Any], order_calls: set[str], cleared_calls: set[tuple[str, str]]
) -> dict[str, Any]:
    """A ``tool_calls`` event with the arguments of each possible order call
    and each secretary call emptied.

    Emptied rather than dropped, as the stream does for arguments it cannot
    parse: the call keeps its card and its id, so its result still lands.
    """
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        return data
    data["tool_calls"] = [
        {**call, "args": {}}
        if isinstance(call, dict)
        and (
            call.get("name") in _SECRETARY_TOOL_NAMES
            or _may_be_order(call, data.get("id"), order_calls, cleared_calls)
        )
        else call
        for call in calls
    ]
    return data


def _track_secretary_calls(data: dict[str, Any], secretary_calls: set[str]) -> None:
    """Record which call ids in a ``tool_calls`` event now name a secretary call."""
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict) or not call.get("id"):
            continue
        if call.get("name") in _SECRETARY_TOOL_NAMES:
            secretary_calls.add(str(call["id"]))
        else:
            secretary_calls.discard(str(call["id"]))


def _without_owner_ids(value: Any) -> Any:
    """A copy of ``value`` with every owner id key removed, at any depth."""
    if isinstance(value, dict):
        return {
            k: _without_owner_ids(v)
            for k, v in value.items()
            if k not in _OWNER_ID_KEYS
        }
    if isinstance(value, list):
        return [_without_owner_ids(v) for v in value]
    return value


def _strip_private_artifact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop the owner-only keys from a tool artifact, in place on the copy."""
    artifact = data.get("artifact")
    if not isinstance(artifact, dict):
        return data
    if _is_order_result(data):
        # The vendor's own answer to an order names the account and the fill.
        data["content"] = ""
    if any(key in artifact for key in _PRIVATE_ARTIFACT_KEYS):
        data["artifact"] = {
            k: v for k, v in artifact.items() if k not in _PRIVATE_ARTIFACT_KEYS
        }
    return data


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
    order_calls = _order_call_ids(stored_events)
    cleared_calls = _cleared_call_ids(stored_events)

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
        # Call ids whose latest call is a secretary one. A result names only
        # its call id, which a provider may repeat later, so this follows the
        # stream in order rather than reading the thread up front.
        secretary_calls: set[str] = set()

        for q in queries:
            if not isinstance(q, dict):
                continue

            turn_index = q.get("turn_index")
            seq += 1

            # Build user_message payload, less the keys a viewer must not read
            metadata = q.get("metadata") or {}
            if isinstance(metadata, dict):
                # Attached context is client-shaped, so an owner id can sit at
                # any depth in it.
                metadata = _without_owner_ids(
                    {
                        k: v
                        for k, v in metadata.items()
                        if k not in _PRIVATE_QUERY_METADATA_KEYS
                    }
                )

            payload = {
                "thread_id": thread_id,
                "turn_index": turn_index,
                "content": q.get("content"),
                "timestamp": q.get("created_at"),
                "metadata": metadata,
            }
            # Tag system queries so the frontend can hide the user bubble. Its
            # text is the agent's own prompt, never shown, and a report-back
            # names the dispatched thread and its workspace, so it stays here.
            query_type = q.get("type")
            if query_type == "system":
                payload["query_type"] = "system"
                payload["content"] = ""
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
                if event_type in _DROPPED_EVENTS:
                    continue

                seq += 1
                # Shallow-copy so we never mutate the stored/cached event dict.
                replay_data = dict(data)
                # The owner's ids never reach a public viewer. Stored
                # workspace_status events carry the workspace id at the top
                # level, tool artifacts (chart annotations) nest it, and a
                # steering event names the user on each message, so they go at
                # every depth. sandbox_state is server-side runtime state.
                replay_data = _without_owner_ids(replay_data)
                replay_data.pop("sandbox_state", None)
                replay_data = _strip_private_artifact(replay_data)
                if event_type == "tool_calls":
                    _track_secretary_calls(replay_data, secretary_calls)
                    replay_data = _strip_call_args(
                        replay_data, order_calls, cleared_calls
                    )
                if (
                    event_type == "tool_call_result"
                    and str(replay_data.get("tool_call_id")) in secretary_calls
                ):
                    replay_data["content"] = ""
                if event_type == "interrupt":
                    # An interrupt asks the owner, and no share renders or
                    # answers one.
                    seq -= 1
                    continue
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
