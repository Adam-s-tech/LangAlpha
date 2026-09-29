"""Tier 1 offloads are a view over the checkpoint, applied from recorded ids.

The batch gate used to decide both when to offload and whether the model saw
the offload: a call truncated at one batch came back in full on the next call,
busting the prompt cache each time. Manual /offload rewrote checkpoint messages
instead, which could race a live turn's appends. Now the id sets are the only
record, every model call re-applies them, and nothing rewrites messages.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ptc_agent.agent.middleware.compaction.compact import offload_tool_args
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware
from ptc_agent.agent.middleware.compaction.offloading import apply_recorded_offloads


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
        # The default counter downloads its encoding on first use.
        token_counter=len,
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


class _FlakySandbox:
    """Backend whose arg writes fail for the call ids in ``failing``."""

    def __init__(self, failing: set[str]):
        self.failing = failing

    async def awrite(self, path, content, *, overwrite=False):
        call_id = path.rsplit("truncated_args_", 1)[1].removesuffix(".md")
        return SimpleNamespace(error="unreachable" if call_id in self.failing else None)


@pytest.mark.asyncio
async def test_failed_arg_write_is_not_recorded_and_retries():
    # A recorded id is truncated on every call and never retried, so recording
    # a failed write left a marker naming a file that does not exist.
    sandbox = _FlakySandbox({"w0"})
    mw = CompactionMiddleware(
        model=GenericFakeChatModel(messages=iter([])),
        trigger=("tokens", 10_000_000),
        keep=("messages", 5),
        summary_prompt="x",
        token_counter=len,
        backend=sandbox,
        truncate_args_settings={
            "trigger": ("messages", 40),
            "keep": ("messages", 10),
            "max_length": 200,
        },
    )
    seen: list[dict] = []

    async def handler(req):
        seen.append(
            {
                tc["id"]: len(tc["args"]["content"])
                for m in req.messages
                if isinstance(m, AIMessage)
                for tc in m.tool_calls
                if tc["name"] == "Write"
            }
        )
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

    assert "w0" not in update["_offloaded_tool_call_ids"]
    assert "w1" in update["_offloaded_tool_call_ids"]
    assert seen[0]["w0"] == 5000 and seen[0]["w1"] < 200

    # The sandbox is back by the next batch: the unrecorded call is retried.
    sandbox.failing = set()
    update = await call(_history(28), update)
    assert "w0" in update["_offloaded_tool_call_ids"]
    assert seen[1]["w0"] < 200

    # Manual /offload records the same way.
    sandbox.failing = {"w0"}
    result = await offload_tool_args(_history(12), backend=sandbox)
    assert "w0" not in result["offloaded_arg_ids"]
    assert result["offloaded_args"] == len(result["offloaded_arg_ids"]) > 0


def test_legacy_merged_id_sets_only_hit_their_own_tool():
    # Older /offload merged arg and read ids into both sets.
    ids = {"w0", "r0"}
    out = {m.id: m for m in apply_recorded_offloads(_history(1), ids, ids, 200, "…")}

    assert out["tw0"].content == "wrote"
    assert out["tr0"].content.startswith("... [this tool call's read result")
    assert len(out["a0"].tool_calls[0]["args"]["content"]) < 200
    assert out["a0"].tool_calls[1]["args"] == {"file_path": "same.py"}
