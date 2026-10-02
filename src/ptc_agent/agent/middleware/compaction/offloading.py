"""Offloading: the view that re-applies recorded offloads to each model call,
and the backend files that keep what truncated args and inline attachments cut."""

import base64
import logging
import uuid
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.config import get_config

from ptc_agent.core.paths import WorkspaceLayout
from src.llms.attachment_payload import FILE_BLOCK_TYPES
from ptc_agent.agent.middleware.compaction.types import TRUNCATABLE_TOOLS
from ptc_agent.agent.middleware.compaction.utils import (
    read_offload_marker,
    strip_base64_from_messages,
    truncate_tool_call,
)

logger = logging.getLogger(__name__)


def tool_call_ids(messages: list[AnyMessage]) -> set[str]:
    return {
        tc["id"]
        for msg in messages
        if isinstance(msg, AIMessage)
        for tc in msg.tool_calls or ()
        if tc.get("id")
    }


def apply_recorded_offloads(
    messages: list[AnyMessage],
    arg_ids: set[str],
    read_ids: set[str],
    max_length: int,
    truncation_text: str,
    thread_dir: str | None = None,
) -> list[AnyMessage]:
    """Re-apply every recorded Tier 1 offload to one model call's messages.

    An offload is a view over the checkpoint, not a rewrite of it: the id sets
    are the record, and every call re-truncates them. The batch gate only
    decides when new ids join; without this, a call truncated at one batch
    came back in full on the next, busting the prompt cache each time. Each id
    is checked against its tool, so an arg id never blanks a result and a read
    id only replaces a Read result.
    """
    if not arg_ids and not read_ids:
        return messages

    read_paths: dict[str, str] = {}
    out: list[AnyMessage] = []
    changed = False
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            calls = []
            msg_changed = False
            for tc in msg.tool_calls:
                if tc["name"] == "Read" and tc["id"] in read_ids:
                    read_paths[tc["id"]] = tc.get("args", {}).get("file_path", "")
                if tc["id"] in arg_ids and tc["name"] in TRUNCATABLE_TOOLS:
                    new_tc = truncate_tool_call(
                        tc, max_length, truncation_text, thread_dir
                    )
                    msg_changed = msg_changed or new_tc is not tc
                    calls.append(new_tc)
                else:
                    calls.append(tc)
            if msg_changed:
                msg = msg.model_copy()
                msg.tool_calls = calls
                changed = True
        elif isinstance(msg, ToolMessage) and msg.tool_call_id in read_paths:
            marker = read_offload_marker(read_paths[msg.tool_call_id])
            if msg.content != marker:
                msg = msg.model_copy()
                msg.content = marker
                changed = True
        out.append(msg)

    return out if changed else messages


def get_thread_id(thread_id: str | None = None) -> str:
    """Short thread id: the one passed, else graph config's, else a session id.

    Manual /compact and /offload run outside the graph, so they pass the id;
    the session fallback is fresh on every call and names no real thread.
    """
    if thread_id:
        return str(thread_id)[:8]
    try:
        config = get_config()
        thread_id = config.get("configurable", {}).get("thread_id")
        if thread_id is not None:
            return str(thread_id)[:8]
    except RuntimeError:
        pass

    return f"session_{uuid.uuid4().hex[:8]}"


async def aoffload_truncated_args(
    backend: Any,
    originals: dict[str, dict[str, Any]],
    *,
    thread_id: str | None = None,
) -> set[str]:
    """Persist original tool call args to sandbox before truncation discards them.

    Each truncated tool call gets its own file at
    `.agents/threads/{tid}/truncated_args_{toolcall_id}.md`.

    Non-fatal -- logs warnings on failure but never raises.

    Args:
        backend: The Daytona backend for filesystem operations.
        originals: Mapping of tool_call_id -> {"name": str, "args": dict}
                   as returned by truncate_message_args.

    Returns:
        The ids safe to record: those written, or all of them with no
        backend, since the marker then names no file. A failed write stays
        out, so its marker never names a missing file and a later pass
        retries it.
    """
    if backend is None:
        return set(originals)

    short_id = get_thread_id(thread_id)
    saved: set[str] = set()

    for tool_call_id, original in originals.items():
        path = WorkspaceLayout.thread_subdir(
            short_id, f"truncated_args_{tool_call_id}.md"
        )
        tool_name = original["name"]
        args = original["args"]

        # Format each arg as a section
        parts = [f"# {tool_name} (call {tool_call_id})\n"]
        for key, value in args.items():
            str_value = str(value) if not isinstance(value, str) else value
            parts.append(f"## {key}\n\n```\n{str_value}\n```\n")

        content = "\n".join(parts)

        try:
            result = await backend.awrite(path, content, overwrite=True)
            if result is None or result.error:
                error_msg = result.error if result else "backend returned None"
                logger.warning(
                    "Failed to offload truncated args for %s (%s): %s",
                    tool_call_id,
                    tool_name,
                    error_msg,
                )
            else:
                saved.add(tool_call_id)
                logger.debug(
                    "Offloaded truncated args for %s (%s) to %s",
                    tool_call_id,
                    tool_name,
                    path,
                )
        except Exception as e:
            logger.warning(
                "Exception offloading truncated args for %s (%s): %s",
                tool_call_id,
                tool_name,
                e,
            )

    return saved


# =============================================================================
# Base64 content offloading
# =============================================================================

# Mime type → file extension mapping
_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


def _extract_base64_info(block: dict) -> tuple[str, str, str] | None:
    """Extract (base64_data, mime_type, label) from a content block.

    Handles every provider format an attachment arrives in: an ``image_url``
    data URI (OpenAI), a ``base64`` key (langchain v1), and a ``source`` object
    (Anthropic native), under either name the block type goes by.

    Returns None if the block doesn't contain base64 data.
    """
    block_type = block.get("type", "")

    # OpenAI-style image_url with data URI
    if block_type == "image_url":
        url = (block.get("image_url") or {}).get("url", "")
        if url.startswith("data:") and ";base64," in url:
            # Parse "data:image/png;base64,<DATA>"
            header, data = url.split(";base64,", 1)
            mime = header.replace("data:", "")
            return data, mime, "image"
        return None

    source = block.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    inline = source.get("data") if source.get("type") == "base64" else None

    # PDF / file upload with inline base64
    if block_type in FILE_BLOCK_TYPES:
        data = block.get("base64") or inline
        if data is None:
            return None
        mime = block.get("mime_type") or source.get("media_type") or "application/pdf"
        fname = block.get("filename", "file")
        return data, mime, f"pdf_{fname}"

    # Anthropic native image block
    if block_type == "image":
        data = block.get("base64") or inline
        if data is not None:
            mime = block.get("mime_type") or source.get("media_type") or "image/png"
            return data, mime, "image"

    return None


async def aoffload_base64_content(
    backend: Any,
    messages: list[AnyMessage],
    *,
    thread_id: str | None = None,
) -> list[AnyMessage]:
    """Offload base64 content blocks to sandbox files, replacing with path references.

    For each message containing base64 content blocks:
    1. Decode the base64 data
    2. Upload to ``.agents/threads/{thread_id}/`` via ``backend.aupload_files``
    3. Replace the block with a text reference to the saved file

    When ``backend`` is None (e.g. flash agent with no sandbox), falls back to
    :func:`strip_base64_from_messages` which replaces base64 with simple
    ``[Image]`` / ``[PDF: name]`` placeholders.

    Args:
        backend: Daytona backend for file uploads, or None.
        messages: Messages potentially containing base64 content blocks.

    Returns:
        New message list with base64 content replaced. Returns the original
        list if no base64 content was found.
    """
    if backend is None:
        return strip_base64_from_messages(messages)

    thread_dir = WorkspaceLayout.thread_subdir(get_thread_id(thread_id))

    result: list[AnyMessage] = []
    changed = False

    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_blocks: list = []
        msg_changed = False
        msg_id = (msg.id or uuid.uuid4().hex)[:8]

        for idx, block in enumerate(content):
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            info = _extract_base64_info(block)
            if info is None:
                new_blocks.append(block)
                continue

            b64_data, mime_type, label = info
            ext = _MIME_TO_EXT.get(mime_type, "bin")
            filename = f"{label}_{msg_id}_{idx}.{ext}"
            path = f"{thread_dir}/{filename}"

            try:
                raw_bytes = base64.b64decode(b64_data)
                upload_result = await backend.aupload_files([(path, raw_bytes)])

                if upload_result is None or (
                    hasattr(upload_result, "error") and upload_result.error
                ):
                    error_msg = (
                        upload_result.error
                        if upload_result and hasattr(upload_result, "error")
                        else "backend returned None"
                    )
                    logger.warning(
                        "Failed to offload base64 block %d of message %s: %s",
                        idx,
                        msg_id,
                        error_msg,
                    )
                    # Fall back to simple placeholder
                    if "pdf" in label:
                        new_blocks.append({"type": "text", "text": f"[PDF: {label}]"})
                    else:
                        new_blocks.append({"type": "text", "text": "[Image]"})
                    msg_changed = True
                    continue

                # Success — replace with file path reference
                if ext == "pdf":
                    new_blocks.append(
                        {
                            "type": "text",
                            "text": f"[PDF saved to {path} — use Read to view]",
                        }
                    )
                else:
                    new_blocks.append(
                        {
                            "type": "text",
                            "text": f"[Image saved to {path} — use Read to view]",
                        }
                    )
                msg_changed = True
                logger.debug(
                    "Offloaded base64 block %d of message %s to %s", idx, msg_id, path
                )

            except Exception as e:
                logger.warning(
                    "Exception offloading base64 block %d of message %s: %s",
                    idx,
                    msg_id,
                    e,
                )
                # Fall back to simple placeholder
                if "pdf" in label:
                    new_blocks.append({"type": "text", "text": f"[PDF: {label}]"})
                else:
                    new_blocks.append({"type": "text", "text": "[Image]"})
                msg_changed = True

        if msg_changed:
            copy = msg.model_copy()
            copy.content = new_blocks
            result.append(copy)
            changed = True
        else:
            result.append(msg)

    return result if changed else messages
