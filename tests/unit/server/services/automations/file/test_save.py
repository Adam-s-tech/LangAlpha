"""The save as a whole: one transaction, the version checks, what a partial
Read may delete, and the report."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from ptc_agent.agent.backends.db_json_route import UserDataValidationError
from src.server.services.automations import lifecycle
from tests.unit.server.services.automations.file._support import (
    AUTOMATIONS,
    BRIEF,
    CALL,
    CREATED,
    FOREIGN_WORKSPACE,
    NEW,
    NEXT_RUN,
    NEXT_RUN_LOCAL,
    OTHER,
    PASSED,
    PATH,
    UNKNOWN_THREAD,
    USER,
    _doc,
    _entries,
    _once_row,
    _refusal,
    _row,
    _write,
)


class TestWholeWrite:
    @pytest.mark.asyncio
    async def test_the_report_names_every_change(self, db, backend):
        db.add(_row(), _row(automation_id=UUID(OTHER), name="Evening wrap"))
        brief, _ = _entries(db.list())

        report = await _write(
            backend,
            [
                {**brief, "instruction": "Summarize the news.", "max_failures": 5},
                {"name": "Weekly", "cron_expression": "0 9 * * 1", "instruction": "Go."},
            ],
        )

        assert report.splitlines() == [
            "Saved automations.json: 1 created, 1 updated, 1 deleted.",
            f'- deleted "Evening wrap" ({OTHER}), with its run history',
            f'- updated "Morning brief" ({BRIEF}): instruction, max_failures; next run {NEXT_RUN_LOCAL}',
            f'- created "Weekly" ({CREATED}); next run {NEXT_RUN_LOCAL}',
            "Read automations.json again before your next edit.",
        ]

    @pytest.mark.asyncio
    async def test_each_change_runs_in_the_saves_transaction_under_its_own_savepoint(self, db, backend):
        db.add(_row(), _row(automation_id=UUID(OTHER), name="Evening wrap"))
        brief, _ = _entries(db.list())

        await _write(backend, [{**brief, "instruction": "Summarize the news.", "status": "paused"}, NEW])

        assert [(w.op, w.depth) for w in db.writes] == [("delete", 1), ("update", 2), ("update", 2), ("create", 2)]
        assert all(w.joined for w in db.writes)

    @pytest.mark.asyncio
    async def test_the_document_as_read_is_no_change(self, db, backend):
        db.add(_row(last_execution={"status": "completed", "completed_at": NEXT_RUN}))
        served = await backend.aread_range(PATH)

        result = await backend.awrite_text(PATH, served)

        assert result["message"].startswith("No changes")
        assert db.writes == []

    @pytest.mark.asyncio
    async def test_a_write_over_a_stale_read_is_a_conflict(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())
        await backend.aread_range(PATH)
        db.rows[BRIEF]["instruction"] = "Changed on the Automations page."

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(PATH, _doc([{**entry, "name": "Renamed"}]))

        assert exc.value.error_type == "version_conflict"
        assert db.writes == []
        assert db.rows[BRIEF]["name"] == "Morning brief"

    @pytest.mark.asyncio
    async def test_an_edit_that_lands_while_the_save_waits_for_the_lock_is_a_conflict(self, db, backend):
        """The version is checked on rows read under the lock: an edit on the
        Automations page that commits while the save waits would otherwise be
        overwritten."""
        db.add(_row())
        [entry] = _entries(db.list())
        await backend.aread_range(PATH)
        db.on_lock = lambda: db.rows[BRIEF].update(instruction="Changed on the Automations page.")

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(PATH, _doc([{**entry, "name": "Renamed"}]))

        assert exc.value.error_type == "version_conflict"
        assert db.writes == []

    @pytest.mark.asyncio
    async def test_an_edit_that_lands_before_the_save_locks_its_rows_is_a_conflict(self, db, backend):
        """The rows are locked only once the save has changes to make, so the
        version is checked again on them as locked: an edit that commits in
        between would otherwise be overwritten."""
        db.add(_row())
        [entry] = _entries(db.list())
        await backend.aread_range(PATH)
        db.on_row_lock = lambda: db.rows[BRIEF].update(instruction="Changed on the Automations page.")

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(PATH, _doc([{**entry, "name": "Renamed"}]))

        assert exc.value.error_type == "version_conflict"
        assert db.writes == []

    @pytest.mark.asyncio
    async def test_a_save_that_changes_nothing_locks_no_row(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        await _write(backend, [{**entry, "state": {"failure_count": 5}}])

        assert not any("FOR UPDATE" in sql for sql in db.sql)

    @pytest.mark.asyncio
    async def test_several_problems_refuse_the_whole_write_at_once(self, db, backend):
        """One retry can fix everything, and the valid create beside the
        refusals is held back with them."""
        db.add(_row())
        entries = [
            {"automation_id": BRIEF, "instruction": None},
            {"name": "Fine", "cron_expression": "0 9 * * *", "instruction": "Go."},
            {"name": "Odd", "colour": "red"},
            {"name": "Bad clock", "cron_expression": "0 9 * * *", "instruction": "Go.", "timezone": "Mars/Base"},
        ]

        error = await _refusal(backend, db, entries)

        assert error.message.splitlines()[0] == "schema_error:automations.json: 3 problems, nothing was saved."
        assert [path for path, _ in error.problems] == [
            "automations[0].instruction",
            'automations[2] ("Odd").colour',
            'automations[3] ("Bad clock").timezone',
        ]
        lifecycle.create_automation.assert_not_awaited()
        assert db.writes == []

    @pytest.mark.asyncio
    async def test_a_change_the_lifecycle_refuses_rolls_the_whole_save_back(self, db, backend):
        """Each change runs the lifecycle's rules under its own savepoint, so
        every entry's first refusal is reported together, and the changes that
        passed are undone with the rest."""
        db.add(
            _once_row(status="paused", next_run_at=PASSED),
            _row(automation_id=UUID(OTHER), name="Evening wrap"),
        )
        brief, _ = _entries(db.list())

        error = await _refusal(
            backend,
            db,
            [
                # Its resume would be refused too, since its time has passed.
                {**brief, "workspace_id": FOREIGN_WORKSPACE, "status": "active"},
                {**NEW, "name": "Stray", "thread": UNKNOWN_THREAD},
                {**NEW, "name": "Fine"},
            ],
        )

        assert error.problems == [
            ('automations[0] ("Morning brief").workspace_id', "belongs to another user"),
            ('automations[1] ("Stray").thread', "not found"),
        ]
        lifecycle.resume_automation.assert_not_awaited()
        assert lifecycle.create_automation.await_count == 2
        assert [(w.op, w.automation_id) for w in db.writes] == [("delete", OTHER), ("create", CREATED)]


class TestPartialRead:
    """A Write replaces the whole document, so one based on a Read that showed
    only part of the file would delete the automations it never showed."""

    @pytest.mark.asyncio
    async def test_a_delete_after_a_partial_read_is_refused(self, db, backend):
        db.add(_row(), _row(automation_id=UUID(OTHER), name="Evening wrap"))
        keep, _ = _entries(db.list())

        error = await _refusal(backend, db, [keep], whole=False)

        assert error.error_type == "incomplete_read"
        assert f'"Evening wrap" ({OTHER})' in error.message
        assert "Edit" in error.message
        assert db.writes == []

    @pytest.mark.asyncio
    async def test_a_partial_read_still_updates_and_creates(self, db, backend):
        db.add(_row())
        [brief] = _entries(db.list())

        report = await _write(
            backend,
            [{**brief, "instruction": "Summarize the news."}, {"name": "W", "cron_expression": "0 9 * * 1", "instruction": "Go."}],
            whole=False,
        )

        assert report.startswith("Saved automations.json: 1 created, 1 updated.")


class TestServedState:
    """``state`` is checked against what the writer was shown: the Read's
    document, or the live rows for a save through the file mount."""

    @pytest.mark.asyncio
    async def test_without_a_served_document_state_is_checked_against_the_live_rows(self):
        rows = [_row(failure_count=2)]
        content, _ = AUTOMATIONS.render(rows)
        edited = json.loads(content)
        edited["automations"][0]["state"]["failure_count"] = 0

        unchanged = AUTOMATIONS.plan(CALL, await AUTOMATIONS.parse(USER, CALL, content, None), rows).changes
        noted = AUTOMATIONS.plan(CALL, await AUTOMATIONS.parse(USER, CALL, json.dumps(edited), None), rows).changes

        assert (bool(unchanged), unchanged.notes) == (False, [])
        assert (bool(noted), noted.notes) == (
            False,
            ['- note: automations[0] ("Morning brief"): state is kept by the server, so what you wrote in it was ignored'],
        )

    @pytest.mark.asyncio
    async def test_a_save_through_the_mount_deletes_without_a_read(self, db, backend):
        db.add(_row(), _row(automation_id=UUID(OTHER), name="Evening wrap"))
        content, version = await backend.aread_versioned(PATH)
        brief, _ = json.loads(content)["automations"]

        stored = await backend.awrite_versioned(
            PATH, _doc([{**brief, "state": {"next_run_at": "2030-01-01T08:30:00-05:00"}}]), version
        )
        report = stored.report

        assert list(db.rows) == [BRIEF]
        # The file as the save left it, which the mount holds next.
        assert (stored.content, stored.version) == await backend.aread_versioned(PATH)
        assert f'- deleted "Evening wrap" ({OTHER}), with its run history' in report.splitlines()
        assert '- note: automations[0] ("Morning brief"): state is kept by the server' in report


class TestLongLists:
    @pytest.mark.asyncio
    async def test_the_deletes_a_partial_read_would_make_are_counted_past_ten(self, db, backend):
        db.add(*(_row(automation_id=UUID(int=i), name=f"Auto {i}") for i in range(1, 26)))

        error = await _refusal(backend, db, [], whole=False)

        assert "and 15 more" in error.message


class TestDeliveryNote:
    @pytest.mark.parametrize(("url", "noted"), [("", True), ("https://hooks.example.com/x", False)])
    @pytest.mark.asyncio
    async def test_delivery_without_a_webhook_is_called_out(self, db, backend, monkeypatch, url, noted):
        monkeypatch.setattr(lifecycle, "settings", MagicMock(AUTOMATION_WEBHOOK_URL=url))
        db.add(_row())
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "delivery": ["slack"]}])

        assert ("Note: Delivery was saved, but AUTOMATION_WEBHOOK_URL is not configured" in report) is noted
