"""Builders shared by the replay suites: checkpoint turns, stored rows, a mocked reader."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import HumanMessage

from src.server.services.history.reader import TaskHistory, TurnSlice

THREAD = "thread-r"


def _turn(ordinal, messages, user="hello", turn_index=None, new_ui_records=None):
    return TurnSlice(
        turn_ordinal=ordinal,
        input_checkpoint_id=f"cp-in-{ordinal}",
        end_checkpoint_id=f"cp-end-{ordinal}",
        user_message=HumanMessage(content=user, id=f"h-{ordinal}"),
        messages=messages,
        turn_index=turn_index,
        new_ui_records=new_ui_records or [],
    )


def _query(turn_index, content="hello", qtype="user"):
    return {"turn_index": turn_index, "content": content, "type": qtype, "created_at": "t0"}


def _response(turn_index, sse_events=None, status="completed"):
    return {
        "conversation_response_id": f"resp-{turn_index}",
        "sse_events": sse_events or [],
        "status": status,
    }


def _mock_reader(monkeypatch, history, task_messages=None, task_history=None):
    reader = MagicMock()
    reader.aget_thread_history = AsyncMock(return_value=history)
    reader.aget_task_history = AsyncMock(
        return_value=task_history or TaskHistory(messages=task_messages or [])
    )
    monkeypatch.setattr(
        "src.server.services.history.replay.CheckpointHistoryReader.get_instance",
        lambda: reader,
    )
    return reader


def _cache_probe(monkeypatch):
    """Absorb cache writes; return the list of cached tail checkpoint ids."""
    from src.server.services.history import replay as replay_module
    from src.server.services.history.replay import task_lane as task_lane_module

    async def fake_details(thread_id, task_ids):
        return {}

    async def fake_live(thread_id, task_ids):
        return set()

    cached: list[str] = []

    async def fake_store(thread_id, tail_checkpoint_id, fingerprint, items):
        cached.append(tail_checkpoint_id)

    async def fake_delete(thread_id, turn_keys):
        pass

    monkeypatch.setattr(task_lane_module, "resolve_task_details", fake_details)
    monkeypatch.setattr(
        replay_module.projection_cache, "live_task_streams", fake_live
    )
    monkeypatch.setattr(replay_module.projection_cache, "store_turn", fake_store)
    monkeypatch.setattr(replay_module.projection_cache, "delete_turns", fake_delete)
    return cached
