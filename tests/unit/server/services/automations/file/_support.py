"""What the automations file's tests share: the rows they start from, and a
save as the agent's Write makes it."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from ptc_agent.agent.backends.automations import AutomationsBackend
from ptc_agent.agent.backends.db_json_route import UserDataValidationError
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.server.services.automations.file import AutomationsFile
from tests.unit.server.services.automations._fake_db import FakeAutomationsDb

AUTOMATIONS = AutomationsFile()

USER = "user-fake-1"
STRANGER = "user-fake-2"
WORKSPACE = "00000000-0000-4000-8000-00000000aaaa"
FOREIGN_WORKSPACE = "00000000-0000-4000-8000-00000000abab"
THREAD = "00000000-0000-4000-8000-00000000bbbb"
PINNED = "00000000-0000-4000-8000-00000000cccc"
UNKNOWN_THREAD = "00000000-0000-4000-8000-00000000dddd"
BRIEF = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
CREATED = "00000000-0000-4000-8000-0000000000ff"
CREATED_NEXT = "00000000-0000-4000-8000-0000000000fe"

ROOT = "/home/workspace/.agents/user/automations"
PATH = f"{ROOT}/automations.json"

# 09:00 in New York, the clock the fixtures' automations run on.
NEXT_RUN = datetime(2030, 10, 1, 13, 0, tzinfo=UTC)
NEXT_RUN_LOCAL = "2030-10-01T09:00:00-04:00"
PASSED = datetime(2021, 5, 1, 13, 0, tzinfo=UTC)

CALL = CallContext(workspace_id=WORKSPACE, thread_id=THREAD, timezone="America/New_York")
NO_ZONE = CallContext(workspace_id=WORKSPACE, thread_id=THREAD)

NEW = {"name": "A", "cron_expression": "0 9 * * *", "instruction": "Go."}


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "automation_id": UUID(BRIEF),
        "name": "Morning brief",
        "description": "weekday digest",
        "status": "active",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "next_run_at": NEXT_RUN,
        "trigger_config": None,
        "timezone": "America/New_York",
        "instruction": "Summarize the watchlist.",
        "agent_mode": "flash",
        "workspace_id": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "llm_model": None,
        "delivery_config": None,
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "last_execution": None,
    }
    row.update(overrides)
    return row


def _once_row(**overrides: Any) -> dict[str, Any]:
    return _row(trigger_type="once", cron_expression=None, **overrides)


def _price_row(**overrides: Any) -> dict[str, Any]:
    config = {"symbol": "AAPL", "conditions": [{"type": "price_below", "value": 200}]}
    return _row(
        trigger_type="price", cron_expression=None, next_run_at=None, trigger_config=config, **overrides
    )


def _entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content, _ = AUTOMATIONS.render(rows)
    return json.loads(content)["automations"]


def _doc(entries: list[Any]) -> str:
    return json.dumps({"automations": entries})


def _backend(call: CallContext) -> AutomationsBackend:
    return AutomationsBackend(user_id=USER, call=call, sandbox_backend=MagicMock(), root_prefix=ROOT)


async def _write(backend: AutomationsBackend, entries: list[Any], *, whole: bool = True) -> str:
    """Read the file as the rows stand, then Write ``entries`` over that Read."""
    await backend.aread_range(PATH, 0, 2000 if whole else 5)
    result = await backend.awrite_text(PATH, _doc(entries))
    return result["message"]


async def _refusal(
    backend: AutomationsBackend, db: FakeAutomationsDb, entries: list[Any], *, whole: bool = True
) -> UserDataValidationError:
    before = copy.deepcopy(db.rows)
    with pytest.raises(UserDataValidationError) as exc:
        await _write(backend, entries, whole=whole)
    assert db.rows == before  # nothing was saved
    return exc.value
