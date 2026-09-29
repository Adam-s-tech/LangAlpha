"""Values that would run differently than the agent meant, refused with the fix."""

from __future__ import annotations


import pytest

from ptc_agent.agent.backends.db_json_route import UserDataValidationError
from src.server.services.automations import lifecycle
from tests.unit.server.services.automations.file._support import (
    BRIEF,
    CREATED,
    NEW,
    NEXT_RUN,
    PASSED,
    PATH,
    ROOT,
    _doc,
    _entries,
    _once_row,
    _refusal,
    _row,
    _write,
)


class TestMisreadValues:
    """Values that parse but would run differently than the agent meant are
    refused with the fix, since a saved report reads as success."""

    @pytest.mark.parametrize(
        ("cron", "says"),
        [("0 0 9 * * 5", "has 6 fields"), ("*/90 * * * *", "/90 step"), ("0 */36 * * *", "/36 step")],
    )
    @pytest.mark.asyncio
    async def test_cron_read_another_way_is_refused(self, db, backend, cron, says):
        error = await _refusal(backend, db, [{**NEW, "cron_expression": cron}])

        assert says in error.hint

    @pytest.mark.parametrize("cron", ["*/60 * * * *", "0 */24 * * *", "@daily", "*/15 9-16 * * 1-5"])
    @pytest.mark.asyncio
    async def test_cron_that_means_what_it_says_is_saved(self, db, backend, cron):
        await _write(backend, [{**NEW, "cron_expression": cron}])

        assert db.rows[CREATED]["cron_expression"] == cron

    @pytest.mark.asyncio
    async def test_a_repeated_key_is_refused_rather_than_last_one_wins(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())
        content = _doc([entry]).replace('"status": "active"', '"status": "paused", "status": "active"')
        await backend.aread_range(PATH)

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(PATH, content)

        assert exc.value.error_type == "parse_error"
        assert '"status" appears twice in "Morning brief"' in exc.value.hint
        assert db.writes == []

    @pytest.mark.parametrize(("value", "says"), [("2030-10-01", "no time of day"), ("2020-01-01T09:00:00", "already passed")])
    @pytest.mark.asyncio
    async def test_a_one_time_run_needs_a_future_time_of_day(self, db, backend, value, says):
        error = await _refusal(backend, db, [{"name": "A", "next_run_at": value, "instruction": "Go."}])

        [(path, message)] = error.problems
        assert path.endswith(".next_run_at") and says in message

    @pytest.mark.asyncio
    async def test_moving_a_one_time_run_into_the_past_is_refused(self, db, backend):
        db.add(_once_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "next_run_at": "2021-05-01T09:00:00"}])

        assert "already passed" in error.hint

    @pytest.mark.parametrize("zone", ["EST", "MST"])
    @pytest.mark.asyncio
    async def test_a_fixed_offset_zone_is_refused_for_its_region(self, db, backend, zone):
        db.add(_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "timezone": zone}])

        assert error.field_path == "automations[0].timezone"
        assert "daylight saving" in error.hint and "America/" in error.hint

    @pytest.mark.asyncio
    async def test_a_new_automation_on_a_fixed_offset_zone_is_refused(self, db, backend):
        error = await _refusal(backend, db, [{**NEW, "timezone": "EST"}])

        assert error.field_path == 'automations[0] ("A").timezone'
        assert "use a region zone such as America/New_York" in error.hint

    @pytest.mark.asyncio
    async def test_a_fixed_offset_zone_the_row_already_has_passes(self, db, backend):
        """The page may store one; resent unchanged, it is not the agent's to fix."""
        db.add(_row(timezone="EST"))
        [brief] = _entries(db.list())

        await _write(backend, [{**brief, "instruction": "Other."}])

        assert (db.rows[BRIEF]["instruction"], db.rows[BRIEF]["timezone"]) == ("Other.", "EST")

    @pytest.mark.asyncio
    async def test_resuming_a_passed_one_time_run_says_to_move_it(self, db, backend):
        """The lifecycle refuses it at commit, against the row as it stands."""
        db.add(_once_row(status="paused", next_run_at=PASSED))

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "status": "active"}])

        assert error.field_path == "automations[0].status"
        assert "set next_run_at to a future time" in error.hint

    @pytest.mark.asyncio
    async def test_resuming_with_a_new_time_saves_both(self, db, backend):
        db.add(_once_row(status="paused", next_run_at=PASSED))

        report = await _write(backend, [{"automation_id": BRIEF, "status": "active", "next_run_at": "2030-10-01T09:00:00"}])

        assert "next_run_at; resumed" in report
        lifecycle.resume_automation.assert_awaited_once()
        assert (db.rows[BRIEF]["status"], db.rows[BRIEF]["next_run_at"]) == ("active", NEXT_RUN)

    @pytest.mark.parametrize("field", ["name", "instruction"])
    @pytest.mark.asyncio
    async def test_an_empty_name_or_instruction_is_refused(self, db, backend, field):
        error = await _refusal(backend, db, [{**NEW, field: ""}])

        assert error.field_path.endswith(f".{field}")


class TestReadmePointer:
    @pytest.mark.asyncio
    async def test_a_refused_document_points_at_the_readme(self, db, backend):
        db.add(_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "instruction": None}])

        assert error.message.splitlines()[-1] == f"See {ROOT}/README.md for the fields and examples."

    @pytest.mark.asyncio
    async def test_a_refusal_of_the_readme_does_not_point_at_the_readme(self, backend):
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(f"{ROOT}/README.md", "# notes")

        assert "See " not in exc.value.message
