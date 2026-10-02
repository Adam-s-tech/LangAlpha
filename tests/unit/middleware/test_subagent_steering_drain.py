"""SubagentSteeringMiddleware delivery: one step takes what both producers queued.

The user's instruction (the steer POST) and the main agent's Task update push
into the same run-scoped queue, and the subagent reads everything drained in
one step as a single newline-joined instruction. The delivery also names each
entry it took, in the captured event and in the stamp checkpoint replay
re-emits, so a client settles its own instruction by id however the text was
joined.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ptc_agent.agent.middleware.background_subagent.middleware import (
    current_background_tool_call_id,
)
from ptc_agent.agent.middleware.background_subagent.redis_stream import (
    steering_queue_key,
)
from ptc_agent.agent.middleware.background_subagent.steering import (
    SubagentSteeringMiddleware,
)
from src.server.services.history.projector import (
    history_events_to_sse,
    messages_to_history_events,
)
from tests.unit.server.handlers.chat.redis_fakes import FakeCache

_CALL = "call-1"
_RUN = "run-A"


class _Registry:
    def __init__(self) -> None:
        self._tasks = {_CALL: SimpleNamespace(task_id="k7Xm2p", task_run_id=_RUN)}
        self.events: list[dict] = []

    async def append_captured_event(self, tool_call_id: str, event: dict) -> None:
        self.events.append(event)


def _entry(content: str, input_id: str) -> str:
    return json.dumps(
        {"content": content, "expected_task_run_id": _RUN, "input_id": input_id}
    )


async def _drain(cache: FakeCache, registry: _Registry):
    token = current_background_tool_call_id.set(_CALL)
    try:
        with patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache):
            return await SubagentSteeringMiddleware(registry).abefore_model(
                {}, MagicMock()
            )
    finally:
        current_background_tool_call_id.reset(token)


ENTRIES = [
    {"input_id": "u-1", "content": "Focus on margins"},
    {"input_id": "m-1", "content": "Also cover 2024 guidance"},
]


@pytest.fixture
def mixed_queue() -> FakeCache:
    """The user's instruction, then the main agent's follow-up, same run."""
    cache = FakeCache()
    cache.client.lists[steering_queue_key(_CALL, _RUN)] = [
        _entry(e["content"], e["input_id"]) for e in ENTRIES
    ]
    return cache


@pytest.mark.asyncio
async def test_delivery_names_each_entry_it_drained(mixed_queue):
    registry = _Registry()

    result = await _drain(mixed_queue, registry)

    (msg,) = result["messages"]
    assert msg.content.endswith("\nFocus on margins\nAlso cover 2024 guidance")
    assert msg.additional_kwargs["steering_delivered"]["entries"] == ENTRIES
    (event,) = [e for e in registry.events if e["event"] == "steering_delivered"]
    assert event["data"]["content"] == "Focus on margins\nAlso cover 2024 guidance"
    assert event["data"]["entries"] == ENTRIES


@pytest.mark.asyncio
async def test_checkpoint_replay_carries_the_entries(mixed_queue):
    """A thread reloaded from the checkpoint, with no captured stream, still
    names each entry."""
    result = await _drain(mixed_queue, _Registry())

    items = history_events_to_sse(
        messages_to_history_events(result["messages"], agent="task:k7Xm2p"),
        thread_id="t-1",
    )

    (item,) = items
    assert item["event"] == "steering_delivered"
    assert item["data"]["agent"] == "task:k7Xm2p"
    assert item["data"]["entries"] == ENTRIES
