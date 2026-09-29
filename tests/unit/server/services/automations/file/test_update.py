"""A changed entry: which fields it updates, and how status pauses and resumes it."""

from __future__ import annotations

from uuid import UUID

import pytest

from src.server.services.automations import lifecycle
from tests.unit.server.services.automations.file._support import (
    BRIEF,
    CREATED,
    NEW,
    NEXT_RUN_LOCAL,
    OTHER,
    PINNED,
    THREAD,
    _entries,
    _once_row,
    _price_row,
    _refusal,
    _row,
    _write,
)


class TestUpdate:
    @pytest.mark.asyncio
    async def test_an_omitted_field_keeps_its_value(self, db, backend):
        db.add(_row(llm_model="model-a"))

        await _write(backend, [{"automation_id": BRIEF, "instruction": "Summarize the news."}])

        lifecycle.update_automation.assert_awaited_once()
        assert lifecycle.update_automation.await_args.args[2] == {"instruction": "Summarize the news."}
        assert (db.rows[BRIEF]["instruction"], db.rows[BRIEF]["llm_model"]) == ("Summarize the news.", "model-a")
        assert [w.op for w in db.writes] == ["update"]

    @pytest.mark.asyncio
    async def test_null_clears_description_and_llm_model(self, db, backend):
        db.add(_row(llm_model="model-a"))
        [entry] = _entries(db.list())

        await _write(backend, [{**entry, "description": None, "llm_model": None}])

        assert lifecycle.update_automation.await_args.args[2] == {"description": None, "llm_model": None}
        assert (db.rows[BRIEF]["description"], db.rows[BRIEF]["llm_model"]) == (None, None)

    @pytest.mark.asyncio
    async def test_null_on_a_required_field_is_refused(self, db, backend):
        db.add(_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "instruction": None}])

        assert error.problems == [("automations[0].instruction", "can't be null")]

    @pytest.mark.asyncio
    async def test_an_edit_to_state_is_ignored_and_noted(self, db, backend):
        """Refusing it cost a whole-document retry when a model "fixed"
        state.next_run_at alongside a schedule change."""
        db.add(_row())
        [entry] = _entries(db.list())

        report = await _write(
            backend, [{**entry, "cron_expression": "30 8 * * 1-5", "state": {"next_run_at": "2030-01-01T08:30:00"}}]
        )

        assert lifecycle.update_automation.await_args.args[2] == {"cron_expression": "30 8 * * 1-5"}
        assert '- note: automations[0] ("Morning brief"): state is kept by the server' in report

    @pytest.mark.asyncio
    async def test_leaving_state_out_is_silent(self, db, backend):
        db.add(_row(failure_count=2))
        [entry] = _entries(db.list())
        del entry["state"]

        report = await _write(backend, [{**entry, "name": "Brief"}])

        assert report.startswith("Saved automations.json: 1 updated.")
        assert "note" not in report

    @pytest.mark.asyncio
    async def test_state_copied_into_a_new_entry_is_ignored(self, db, backend):
        report = await _write(backend, [{**NEW, "state": {}}])

        assert report.startswith("Saved automations.json: 1 created.")
        assert "note" not in report

    @pytest.mark.asyncio
    async def test_a_state_only_edit_is_no_change_with_the_note(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "state": {"failure_count": 0}}])

        assert report.startswith("No changes") and "- note:" in report
        assert db.writes == []

    @pytest.mark.parametrize(
        ("before", "after", "control", "line"),
        [
            ("active", "paused", "pause_automation", f'- updated "Morning brief" ({BRIEF}): paused'),
            (
                "paused",
                "active",
                "resume_automation",
                f'- updated "Morning brief" ({BRIEF}): resumed; next run {NEXT_RUN_LOCAL}',
            ),
            (
                "disabled",
                "active",
                "resume_automation",
                f'- updated "Morning brief" ({BRIEF}): resumed; next run {NEXT_RUN_LOCAL}',
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_pauses_and_resumes(self, db, backend, before, after, control, line):
        db.add(_row(status=before))

        report = await _write(backend, [{"automation_id": BRIEF, "status": after}])

        getattr(lifecycle, control).assert_awaited_once()
        lifecycle.update_automation.assert_not_awaited()
        assert line in report.splitlines()
        assert db.rows[BRIEF]["status"] == after

    @pytest.mark.parametrize(
        ("before", "after"),
        [("active", "disabled"), ("paused", "completed"), ("completed", "active"), ("executing", "paused")],
    )
    @pytest.mark.asyncio
    async def test_any_other_status_change_is_refused(self, db, backend, before, after):
        db.add(_row(status=before))

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "status": after}])

        assert error.field_path == "automations[0].status"

    @pytest.mark.asyncio
    async def test_a_disabled_automation_is_resumed_not_paused(self, db, backend):
        db.add(_row(status="disabled"))

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "status": "paused"}])

        [(path, message)] = error.problems
        assert path == "automations[0].status"
        assert message.startswith("can't go from \"disabled\" to \"paused\":")
        assert '"active" to resume a paused or disabled one' in message

    @pytest.mark.asyncio
    async def test_the_trigger_type_is_fixed(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        error = await _refusal(backend, db, [{**entry, "trigger_type": "once"}])

        assert "trigger_type can't change" in error.hint

    @pytest.mark.parametrize("status", ["paused", "disabled"])
    @pytest.mark.asyncio
    async def test_an_automation_that_is_not_running_reports_no_next_run(self, db, backend, status):
        db.add(_row(status=status))

        report = await _write(backend, [{"automation_id": BRIEF, "instruction": "Summarize the news."}])

        assert f'- updated "Morning brief" ({BRIEF}): instruction' in report.splitlines()

    @pytest.mark.parametrize(
        ("entries", "where", "says"),
        [
            (
                [{"automation_id": BRIEF, "colour": "red"}],
                "automations[0].colour",
                "unknown field; allowed: automation_id, name, description",
            ),
            ([{"automation_id": OTHER, "name": "x"}], 'automations[0] ("x").automation_id', "no automation has this id"),
            (
                [{"automation_id": BRIEF}, {"automation_id": BRIEF}],
                "automations[1].automation_id",
                "appears twice",
            ),
        ],
        ids=["unknown-field", "unknown-id", "duplicate-id"],
    )
    @pytest.mark.asyncio
    async def test_an_entry_that_cannot_name_its_row_is_refused(self, db, backend, entries, where, says):
        db.add(_row())

        error = await _refusal(backend, db, entries)

        [(path, message)] = error.problems
        assert path == where
        assert says in message

    @pytest.mark.asyncio
    async def test_an_automation_left_out_of_the_document_is_deleted(self, db, backend):
        db.add(_row(), _row(automation_id=UUID(OTHER), name="Evening wrap"))
        keep, _ = _entries(db.list())

        report = await _write(backend, [keep])

        assert list(db.rows) == [BRIEF]
        assert [(w.op, w.automation_id) for w in db.writes] == [("delete", OTHER)]
        assert f'- deleted "Evening wrap" ({OTHER}), with its run history' in report.splitlines()


class TestThreadRoundTrip:
    @pytest.mark.parametrize(
        "written",
        ["persistent", PINNED],
        ids=["persistent", "its-own-thread-id"],
    )
    @pytest.mark.asyncio
    async def test_a_persistent_thread_written_back_keeps_its_thread(self, db, backend, written):
        """Writing "persistent" again must not unpin the thread the first run made."""
        db.add(_row(thread_strategy="continue", conversation_thread_id=UUID(PINNED), owns_thread=True))
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "thread": written}])

        assert report.startswith("No changes")

    @pytest.mark.asyncio
    async def test_persistent_over_a_pinned_conversation_moves_to_its_own_thread(self, db, backend):
        db.add(_row(thread_strategy="continue", conversation_thread_id=UUID(PINNED)))
        [entry] = _entries(db.list())

        await _write(backend, [{**entry, "thread": "persistent"}])

        assert lifecycle.update_automation.await_args.args[2] == {
            "thread_strategy": "continue",
            "conversation_thread_id": None,
        }
        assert db.rows[BRIEF]["conversation_thread_id"] is None

    @pytest.mark.asyncio
    async def test_current_on_a_row_pinned_to_this_conversation_is_no_change(self, db, backend):
        db.add(_row(thread_strategy="continue", conversation_thread_id=UUID(THREAD)))
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "thread": "current"}])

        assert report.startswith("No changes")


class TestCompleted:
    """A completed automation never runs on its own again; a new schedule on
    it would be saved and never fire."""

    @pytest.mark.asyncio
    async def test_a_new_time_on_a_completed_one_time_automation_is_refused(self, db, backend):
        db.add(_once_row(status="completed"))
        [entry] = _entries(db.list())

        error = await _refusal(backend, db, [{**entry, "next_run_at": "2030-11-01T09:00:00"}])

        [(path, message)] = error.problems
        assert path.endswith(".next_run_at")
        assert "Add a new entry" in message

    @pytest.mark.asyncio
    async def test_a_new_condition_on_a_completed_price_alert_is_refused(self, db, backend):
        db.add(_price_row(status="completed"))
        [entry] = _entries(db.list())
        config = {"symbol": "AAPL", "conditions": [{"type": "price_below", "value": 180}]}

        error = await _refusal(backend, db, [{**entry, "trigger_config": config}])

        assert error.field_path.endswith(".trigger_config")

    @pytest.mark.asyncio
    async def test_reactivating_a_completed_automation_says_how_to_run_it_again(self, db, backend):
        db.add(_once_row(status="completed"))

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "status": "active"}])

        assert "Add a new entry" in error.hint

    @pytest.mark.asyncio
    async def test_other_fields_of_a_completed_automation_still_save(self, db, backend):
        """Run now on the Automations page can still run it, with the edited instruction."""
        db.add(_once_row(status="completed"))

        await _write(backend, [{"automation_id": BRIEF, "instruction": "Summarize the news."}])

        lifecycle.update_automation.assert_awaited_once()
        assert db.rows[BRIEF]["instruction"] == "Summarize the news."


class TestNulls:
    @pytest.mark.asyncio
    async def test_null_next_run_at_on_a_one_time_automation_is_refused(self, db, backend):
        db.add(_once_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "next_run_at": None}])

        assert '"paused"' in error.hint

    @pytest.mark.asyncio
    async def test_another_kinds_schedule_field_left_null_is_absent(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "next_run_at": None, "trigger_config": None}])

        assert report.startswith("No changes")

    @pytest.mark.asyncio
    async def test_a_create_ignores_another_kinds_null_schedule_field(self, db, backend):
        await _write(backend, [{**NEW, "trigger_config": None, "next_run_at": None}])

        assert db.rows[CREATED]["trigger_type"] == "cron"


class TestValues:
    @pytest.mark.parametrize("status", [["paused"], {"to": "paused"}, 1])
    @pytest.mark.asyncio
    async def test_a_status_that_is_not_a_name_is_refused(self, db, backend, status):
        db.add(_row())

        error = await _refusal(backend, db, [{"automation_id": BRIEF, "status": status}])

        assert error.field_path == "automations[0].status"

    @pytest.mark.asyncio
    async def test_the_same_value_in_another_form_is_no_change(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        report = await _write(backend, [{**entry, "max_failures": "3"}])

        assert report.startswith("No changes")
        lifecycle.update_automation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delivery_may_be_one_comma_separated_string(self, db, backend):
        db.add(_row())
        [entry] = _entries(db.list())

        await _write(backend, [{**entry, "delivery": "slack, email"}])

        assert lifecycle.update_automation.await_args.args[2] == {"delivery_config": {"methods": ["slack", "email"]}}
        assert _entries(db.list())[0]["delivery"] == ["slack", "email"]


class TestModel:
    @pytest.mark.asyncio
    async def test_an_unknown_model_is_refused_with_the_names_to_use(self, db, backend):
        error = await _refusal(backend, db, [{**NEW, "llm_model": "model-unknown"}])

        assert error.field_path.endswith(".llm_model")
        assert "one of: my-local, model-a, model-b" in error.hint

    @pytest.mark.asyncio
    async def test_a_model_the_user_can_run_is_saved(self, db, backend):
        db.add(_row())

        await _write(backend, [{"automation_id": BRIEF, "llm_model": "my-local"}])

        assert lifecycle.update_automation.await_args.args[2] == {"llm_model": "my-local"}
        assert db.rows[BRIEF]["llm_model"] == "my-local"
