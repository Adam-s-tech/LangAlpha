import json
from datetime import datetime, timezone

import pytest

from src.tools.automation.tools import _parse_schedule


def test_one_time_without_offset_reads_on_the_users_clock():
    parsed = _parse_schedule("2030-10-28T14:15:00", "America/New_York")
    assert parsed["trigger_type"] == "once"
    assert parsed["next_run_at"] == datetime(2030, 10, 28, 18, 15, tzinfo=timezone.utc)


def test_one_time_with_offset_keeps_it():
    parsed = _parse_schedule("2030-10-28T14:15:00+00:00", "Asia/Tokyo")
    assert parsed["next_run_at"] == datetime(2030, 10, 28, 14, 15, tzinfo=timezone.utc)


def test_unknown_zone_falls_back_to_utc():
    parsed = _parse_schedule("2030-10-28T14:15:00", "Not/AZone")
    assert parsed["next_run_at"] == datetime(2030, 10, 28, 14, 15, tzinfo=timezone.utc)


def test_a_date_without_a_time_is_refused_rather_than_run_at_midnight():
    with pytest.raises(ValueError) as exc:
        _parse_schedule("2030-01-01", "America/New_York")

    assert str(exc.value).startswith(
        "Invalid schedule: '2030-01-01' has no time of day, which would run at midnight; "
        "write one, e.g. 2030-01-01T09:00:00. Use a cron expression"
    )


@pytest.mark.asyncio
async def test_the_create_tool_hands_the_date_only_refusal_to_the_agent(monkeypatch):
    from src.tools.automation import tools

    async def unreachable(*_args, **_kwargs):
        raise AssertionError("a date-only schedule reached the lifecycle")

    monkeypatch.setattr(tools.lifecycle, "create_automation", unreachable)

    content, artifact = await tools.create_automation.coroutine(
        name="A", instruction="x", schedule="2030-01-01",
        config={"configurable": {"user_id": "user-fake-1"}},
    )

    assert "'2030-01-01' has no time of day" in json.loads(content)["error"]
    assert artifact == {}


def test_a_past_one_time_run_is_refused_on_the_users_clock():
    """The scheduler would fire it the moment it is saved."""
    with pytest.raises(ValueError) as exc:
        _parse_schedule("2020-01-01T09:00:00", "Asia/Tokyo")

    assert str(exc.value).startswith("2020-01-01T09:00:00+09:00 has already passed (it is ")
    assert str(exc.value).endswith("on this automation's clock); set a future time")


@pytest.mark.asyncio
async def test_both_tools_hand_the_past_time_refusal_to_the_agent(monkeypatch):
    from src.tools.automation import tools

    async def unreachable(*_args, **_kwargs):
        raise AssertionError("a past time reached the lifecycle")

    monkeypatch.setattr(tools.lifecycle, "create_automation", unreachable)
    monkeypatch.setattr(tools.lifecycle, "update_automation", unreachable)
    config = {"configurable": {"user_id": "user-fake-1"}}

    content, _ = await tools.create_automation.coroutine(
        name="A", instruction="x", schedule="2020-01-01T09:00:00", config=config
    )
    updated = await tools.manage_automation.coroutine(
        automation_id="00000000-0000-4000-8000-000000000001",
        action="update",
        schedule="2020-01-01T09:00:00",
        config=config,
    )

    assert "has already passed" in json.loads(content)["error"]
    assert "has already passed" in updated["error"]


@pytest.mark.asyncio
async def test_a_fixed_offset_profile_zone_is_the_users_own(monkeypatch):
    """The tool takes the zone from the user's profile, not from the agent,
    so the region-zone guard is not the tool's to apply."""
    from src.tools.automation import tools

    created = []

    async def create(user_id, data):
        created.append(data)
        return {
            "automation_id": "00000000-0000-4000-8000-000000000001",
            "name": data.name,
            "status": "active",
            "trigger_type": "cron",
            "cron_expression": data.cron_expression,
        }

    monkeypatch.setattr(tools.lifecycle, "create_automation", create)

    content, _ = await tools.create_automation.coroutine(
        name="A",
        instruction="x",
        schedule="0 9 * * 1-5",
        config={"configurable": {"user_id": "user-fake-1", "timezone": "EST"}},
    )

    assert json.loads(content)["success"] is True
    assert created[0].timezone == "EST"
