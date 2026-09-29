"""Render a checkpointed message list as JSONL, one file per turn.

The transcript is a cache of the checkpoint shaped for ``rg`` and ``Read``: one
event per line, one line per user message, assistant text, tool call or tool
result. Reasoning, compaction summaries and model-facing injections (runtime
rows, market watch, credit gate) are left out, since none is something the
user or the agent said or did.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from ptc_agent.agent.transcript.classify import human_kind, is_run_boundary_message
from src.llms.attachment_payload import FILE_BLOCK_TYPES, IMAGE_BLOCK_TYPES

SCHEMA_VERSION = 1

_TURN_ROW_KIND = "turn_opened"

# The pointer LargeResultEvictionMiddleware leaves in place of a large result.
_EVICTED_POINTER = re.compile(
    r"^Tool result too large, the result of this tool call \S+ was saved in the "
    r"filesystem at this path: (\S+)"
)


@dataclass
class Segment:
    """One turn (or one subagent run) of rendered events."""

    number: int
    lines: list[str] = field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    at: str | None = None

    @property
    def content(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.content.encode()).hexdigest()


def evicted_path(message: ToolMessage) -> str | None:
    text = message.content if isinstance(message.content, str) else _text(message.content)
    match = _EVICTED_POINTER.match(text)
    return match.group(1) if match else None


def _turn_opened_at(message: HumanMessage) -> str | None:
    meta = (message.additional_kwargs or {}).get("runtime_update")
    if isinstance(meta, dict) and meta.get("kind") == _TURN_ROW_KIND:
        created = meta.get("created_at")
        return created if isinstance(created, str) else None
    return None


def _text(content: Any, *, attachments: bool = True) -> str:
    """Visible text of a content value; attachments become short placeholders."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get("type", "")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif attachments and kind in IMAGE_BLOCK_TYPES:
                parts.append("[image]")
            elif attachments and kind in FILE_BLOCK_TYPES:
                parts.append(f"[file: {block.get('filename') or 'file'}]")
    return "\n".join(p for p in parts if p)


def split_runs(messages: Iterable[AnyMessage]) -> list[list[AnyMessage]]:
    """Group messages into turns, each opened by a real user message.

    Anything before the first user message (a resumed task's leftovers, an
    old thread's system rows) stays with the first turn.
    """
    runs: list[list[AnyMessage]] = []
    for message in messages:
        if is_run_boundary_message(message) or not runs:
            runs.append([])
        runs[-1].append(message)
    return runs


def render_segment(
    number: int,
    messages: list[AnyMessage],
    *,
    unit: str = "turn",
    tool_names: dict[str, str] | None = None,
) -> Segment:
    """Render one turn."""
    names = tool_names if tool_names is not None else {}
    segment = Segment(number=number)

    def emit(event: dict[str, Any], message: AnyMessage) -> None:
        segment.lines.append(
            json.dumps(
                {"seq": len(segment.lines) + 1, unit: number, **event},
                ensure_ascii=False,
                default=str,
            )
        )
        segment.first_id = segment.first_id or message.id
        segment.last_id = message.id

    for message in messages:
        if isinstance(message, HumanMessage):
            at = _turn_opened_at(message)
            if at and segment.at is None:
                segment.at = at
            kind = human_kind(message)
            if kind not in ("plain", "steering"):
                continue
            event = {"type": "user", "id": message.id, "text": _text(message.content)}
            if kind == "steering":
                event["steering"] = True
            emit(event, message)
        elif isinstance(message, AIMessage):
            text = _text(message.content, attachments=False)
            if text.strip():
                emit({"type": "assistant", "id": message.id, "text": text}, message)
            for call in message.tool_calls or ():
                names[call["id"]] = call["name"]
                emit(
                    {
                        "type": "tool_call",
                        "id": message.id,
                        "call_id": call["id"],
                        "tool": call["name"],
                        "args": call.get("args", {}),
                    },
                    message,
                )
        elif isinstance(message, ToolMessage):
            event = {
                "type": "tool_result",
                "id": message.id,
                "call_id": message.tool_call_id,
                "tool": names.get(message.tool_call_id) or message.name,
                "status": message.status,
                "text": _text(message.content),
            }
            path = evicted_path(message)
            if path:
                event["evicted"] = path
            emit(event, message)

    # The turn row lands right after the user message, so the user event is
    # written before its time is known; stamp it now.
    if segment.at and segment.lines:
        first = json.loads(segment.lines[0])
        if first.get("type") == "user":
            first["at"] = segment.at
            segment.lines[0] = json.dumps(first, ensure_ascii=False, default=str)
    return segment


def render(messages: Iterable[AnyMessage], *, unit: str = "turn") -> list[Segment]:
    """Every turn of a message list, numbered from 1."""
    names: dict[str, str] = {}
    segments = []
    for number, run in enumerate(split_runs(messages), start=1):
        segments.append(render_segment(number, run, unit=unit, tool_names=names))
    return segments


def message_turns(messages: Iterable[AnyMessage]) -> dict[str, int]:
    """The turn each message belongs to, by message id."""
    return {
        message.id: number
        for number, run in enumerate(split_runs(messages), start=1)
        for message in run
        if message.id
    }
