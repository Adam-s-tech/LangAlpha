"""A compaction summary points at the transcript only once its save lands.

The turn-end export runs after the agent finishes, so a pointer kept on a
failed save sent the agent, for the rest of the turn, to history that was not
there, and the trimming note told it the dropped turns existed only there.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware
from ptc_agent.agent.middleware.compaction.types import CONTEXT_SUMMARY_PREFIX
from ptc_agent.agent.transcript import TranscriptTarget, pointer

TRANSCRIPT = TranscriptTarget("abcd1234-0000-0000-0000-000000000000")


class _Mount:
    def __init__(self, outcome):
        self.outcome = outcome

    async def save_transcript(self, target, messages):
        if self.outcome == "raises":
            raise ConnectionError("store down")
        if self.outcome == "hangs":
            await asyncio.sleep(10)
        return self.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [True, False, "raises", "hangs"])
async def test_summary_points_at_transcript_only_when_saved(monkeypatch, outcome):
    monkeypatch.setattr(pointer, "_EXPORT_TIMEOUT", 0.05)
    mw = CompactionMiddleware(
        model=GenericFakeChatModel(messages=iter([AIMessage("the summary")])),
        trigger=("messages", 6),
        keep=("messages", 2),
        summary_prompt="x",
        token_counter=len,
        backend=SimpleNamespace(livefs=_Mount(outcome)),
    )
    monkeypatch.setattr(mw, "_transcript_target", lambda: TRANSCRIPT)
    sent: list = []

    async def handler(req):
        sent.append(req.messages)
        return ModelResponse(result=[AIMessage("done")])

    msgs = [
        m
        for i in range(6)
        for m in (HumanMessage(f"q{i}", id=f"h{i}"), AIMessage(f"a{i}", id=f"a{i}"))
    ]
    req = ModelRequest(
        model=mw.model,
        messages=msgs,
        system_message=None,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={},
        runtime=None,
    )
    update = (await mw.awrap_model_call(req, handler)).command.update

    # Compaction goes ahead either way; only the pointer depends on the save.
    summary = sent[0][0].content
    assert summary.startswith(f"{CONTEXT_SUMMARY_PREFIX}the summary")
    landed = outcome is True
    assert (TRANSCRIPT.directory in summary) is landed
    assert (update["_summarization_event"]["file_path"] is not None) is landed
