"""Tier 1 offloads are a view over the checkpoint, applied from recorded ids.

The batch gate used to decide both when to offload and whether the model saw
the offload: a call truncated at one batch came back in full on the next call,
busting the prompt cache each time. Manual /offload rewrote checkpoint messages
instead, which could race a live turn's appends. Now the id sets are the only
record, every model call re-applies them, and nothing rewrites messages.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ptc_agent.agent.middleware.compaction.compact import offload_tool_args
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware
from ptc_agent.agent.middleware.compaction.utils import apply_recorded_offloads


def _history(pairs: int) -> list:
    msgs = [HumanMessage("go", id="h0")]
    for i in range(pairs):
        msgs.append(
            AIMessage(
                "",
                id=f"a{i}",
                tool_calls=[
                    {"name": "Write", "id": f"w{i}", "args": {"content": "x" * 5000}},
                    {"name": "Read", "id": f"r{i}", "args": {"file_path": "same.py"}},
                ],
            )
        )
        msgs.append(ToolMessage("wrote", tool_call_id=f"w{i}", id=f"tw{i}"))
        msgs.append(ToolMessage("body", tool_call_id=f"r{i}", id=f"tr{i}"))
    return msgs


def _truncated_writes(messages: list) -> int:
    return sum(
        len(tc["args"]["content"]) < 5000
        for m in messages
        if isinstance(m, AIMessage)
        for tc in m.tool_calls
        if tc["name"] == "Write"
    )


@pytest.mark.asyncio
async def test_offload_records_ids_and_never_rewrites_messages():
    msgs = _history(12)
    before = [m.model_dump_json() for m in msgs]

    first = await offload_tool_args(msgs, backend=None)

    assert "messages" not in first
    assert [m.model_dump_json() for m in msgs] == before
    assert first["offloaded_args"] and first["offloaded_reads"]
    assert first["offloaded_arg_ids"].isdisjoint(first["offloaded_read_ids"])

    with pytest.raises(ValueError, match="Nothing to offload"):
        await offload_tool_args(
            msgs,
            backend=None,
            already_offloaded=first["offloaded_arg_ids"],
            already_offloaded_reads=first["offloaded_read_ids"],
        )


@pytest.mark.asyncio
async def test_recorded_offloads_stay_applied_between_batches():
    mw = CompactionMiddleware(
        model=GenericFakeChatModel(messages=iter([])),
        trigger=("tokens", 10_000_000),
        keep=("messages", 5),
        summary_prompt="x",
        truncate_args_settings={
            "trigger": ("messages", 40),
            "keep": ("messages", 10),
            "max_length": 200,
        },
    )
    seen: list[int] = []

    async def handler(req):
        seen.append(_truncated_writes(req.messages))
        return ModelResponse(result=[AIMessage("done")])

    async def call(msgs, state):
        req = ModelRequest(
            model=mw.model,
            messages=msgs,
            system_message=None,
            tool_choice=None,
            tools=[],
            response_format=None,
            state=state,
            runtime=None,
        )
        return (await mw.awrap_model_call(req, handler)).command.update

    update = await call(_history(14), {})
    # Two messages later: below the next batch trigger, so no new offloads.
    await call(_history(15), update)

    assert seen[0] > 0
    assert seen[1] == seen[0]


def test_legacy_merged_id_sets_only_hit_their_own_tool():
    # Older /offload merged arg and read ids into both sets.
    ids = {"w0", "r0"}
    out = {m.id: m for m in apply_recorded_offloads(_history(1), ids, ids, 200, "…")}

    assert out["tw0"].content == "wrote"
    assert out["tr0"].content.startswith("... [this tool call's read result")
    assert len(out["a0"].tool_calls[0]["args"]["content"]) < 200
    assert out["a0"].tool_calls[1]["args"] == {"file_path": "same.py"}
