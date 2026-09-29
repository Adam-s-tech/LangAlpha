"""A new entry: what it defaults to, and what the save reads about the user
to fill those defaults."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tests.unit.server.services.automations.file._support import (
    AUTOMATIONS,
    BRIEF,
    CALL,
    CREATED,
    NEW,
    NEXT_RUN_LOCAL,
    NO_ZONE,
    THREAD,
    USER,
    WORKSPACE,
    _backend,
    _doc,
    _entries,
    _row,
    _write,
)


class TestCreate:
    @pytest.mark.asyncio
    async def test_a_new_entry_takes_the_writers_defaults(self, db, backend):
        await _write(backend, [{"name": "Brief", "cron_expression": "0 9 * * 1-5", "instruction": "Go."}])

        row = db.rows[CREATED]
        assert row["trigger_type"] == "cron"
        assert row["timezone"] == "America/New_York"
        assert row["agent_mode"] == "ptc"
        assert row["workspace_id"] == UUID(WORKSPACE)
        assert (row["thread_strategy"], row["conversation_thread_id"]) == ("new", None)
        assert row["max_failures"] == 3

    @pytest.mark.parametrize(
        ("entry", "kind"),
        [
            ({"next_run_at": "2030-10-01T09:00:00"}, "once"),
            ({"trigger_config": {"symbol": "AAPL", "conditions": [{"type": "price_below", "value": 200}]}}, "price"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_trigger_type_follows_the_schedule_field(self, db, backend, entry, kind):
        await _write(backend, [{"name": "A", "instruction": "Go.", **entry}])

        assert db.rows[CREATED]["trigger_type"] == kind

    @pytest.mark.asyncio
    async def test_thread_current_pins_this_conversation(self, db, backend):
        await _write(backend, [{**NEW, "thread": "current"}])

        row = db.rows[CREATED]
        assert (row["thread_strategy"], row["conversation_thread_id"]) == ("continue", UUID(THREAD))

    @pytest.mark.parametrize(
        ("timezone", "expected"),
        [
            (None, datetime(2030, 10, 1, 13, 0, tzinfo=UTC)),
            ("Asia/Tokyo", datetime(2030, 10, 1, 0, 0, tzinfo=UTC)),
        ],
        ids=["writers-clock", "entrys-own-clock"],
    )
    @pytest.mark.asyncio
    async def test_a_time_without_an_offset_is_read_on_the_automations_clock(self, db, backend, timezone, expected):
        entry = {"name": "A", "next_run_at": "2030-10-01T09:00:00", "instruction": "Go."}
        if timezone:
            entry["timezone"] = timezone

        await _write(backend, [entry])

        assert db.rows[CREATED]["next_run_at"] == expected

    @pytest.mark.parametrize(
        ("entry", "suffix"),
        [
            ({"cron_expression": "0 9 * * *", "status": "paused"}, ", paused"),
            ({"next_run_at": "2030-10-01T09:00:00"}, f"; next run {NEXT_RUN_LOCAL}"),
            (
                {"trigger_config": {"symbol": "AAPL", "conditions": [{"type": "price_below", "value": 200}]}},
                "; watching AAPL as a US stock",
            ),
        ],
        ids=["paused", "once", "price"],
    )
    @pytest.mark.asyncio
    async def test_a_create_reports_when_it_will_run(self, db, backend, entry, suffix):
        report = await _write(backend, [{"name": "New", "instruction": "Go.", **entry}])

        assert f'- created "New" ({CREATED}){suffix}' in report.splitlines()


class TestDefaultZone:
    """A create that names no timezone runs on the conversation's clock, else
    on the user's stored zone, else on UTC. The stored zone costs a query, so
    it is read only when the conversation has none."""

    @pytest.mark.asyncio
    async def test_the_calls_zone_comes_first(self, db):
        document = await AUTOMATIONS.parse(USER, CALL, _doc([NEW]), None)

        assert document.timezone == "America/New_York"
        db.get_user_timezone.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_one_the_users_stored_zone_runs_every_create(self, db):
        document = await AUTOMATIONS.parse(USER, NO_ZONE, _doc([NEW, {**NEW, "name": "B"}]), None)
        plan = AUTOMATIONS.plan(NO_ZONE, document, []).changes

        assert [c.data.timezone for c in plan.creates] == ["Europe/Paris", "Europe/Paris"]
        db.get_user_timezone.assert_awaited_once_with(USER)

    @pytest.mark.asyncio
    async def test_a_create_that_names_a_zone_keeps_it(self, db):
        document = await AUTOMATIONS.parse(USER, NO_ZONE, _doc([{**NEW, "timezone": "Asia/Tokyo"}]), None)
        plan = AUTOMATIONS.plan(NO_ZONE, document, []).changes

        assert plan.creates[0].data.timezone == "Asia/Tokyo"

    @pytest.mark.asyncio
    async def test_with_no_zone_anywhere_it_runs_on_utc(self, db):
        db.get_user_timezone = AsyncMock(return_value=None)

        document = await AUTOMATIONS.parse(USER, NO_ZONE, _doc([NEW]), None)

        assert document.timezone == "UTC"

    @pytest.mark.asyncio
    async def test_a_fixed_offset_default_is_the_users_own_choice(self, db):
        """Only a zone the agent writes is held to region zones."""
        db.get_user_timezone = AsyncMock(return_value="EST")

        await _write(_backend(NO_ZONE), [NEW])

        assert db.rows[CREATED]["timezone"] == "EST"

    @pytest.mark.asyncio
    async def test_a_save_that_creates_nothing_and_names_no_model_reads_nothing_about_the_user(self, db):
        db.add(_row())
        [brief] = _entries(db.list())

        await _write(_backend(NO_ZONE), [{**brief, "instruction": "Summarize the news."}])

        assert db.depths_read_at == []

    @pytest.mark.asyncio
    async def test_what_a_save_reads_about_the_user_is_read_before_its_locks(self, db):
        """Read under the save's locks, the stored zone and the model
        preferences would each hold a second pool connection while other
        saves wait on those locks."""
        db.add(_row())
        [brief] = _entries(db.list())

        await _write(_backend(NO_ZONE), [{**brief, "llm_model": "my-local"}, {**NEW, "llm_model": "model-a"}])

        assert (db.rows[BRIEF]["llm_model"], db.rows[CREATED]["llm_model"]) == ("my-local", "model-a")
        # The stored zone, then the model preferences, each outside any transaction.
        assert db.depths_read_at == [0, 0]
