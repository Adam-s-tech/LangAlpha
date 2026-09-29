"""``AutomationsBackend``: the read-before-write rules of a DB-backed file.

The route plumbing is ``DbJsonRoute``, shared with the profile files. What is
pinned here is what the agent sees through the automations route: a Write is
checked against the Read it follows and may delete only after a Read that
showed the whole file, an Edit lands on what that Read showed, a refused write
sends the agent back to Read, and a write answers with the report of what
changed. A save through the file mount is checked against the route's own
version instead. The automations file is faked at each step the route runs;
its own tests cover what a save does to the rows.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from ptc_agent.agent.backends import ReadOnlyStoreError, db_json_route
from ptc_agent.agent.backends.automations import AUTOMATIONS_FILE, README_FILE, AutomationsBackend
from ptc_agent.agent.backends.db_json_route import Plan, UserDataValidationError
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.server.services.automations.file import AutomationFileError, AutomationsFile, Document, FilePlan, _Delete

PREFIX = "/home/workspace/.agents/user/automations/"
FILE_PATH = f"{PREFIX}{AUTOMATIONS_FILE}"
README_PATH = f"{PREFIX}{README_FILE}"

USER = "user-fake-1"
CALL = CallContext(workspace_id="00000000-0000-4000-8000-00000000aaaa", thread_id=None, timezone="UTC")
DOCUMENT = Document(entries=[], states=None, timezone="UTC", model_pref=None)

# The rows as a read outside a save finds them, as the save reads them, and as
# the save leaves them.
LIVE, ROWS, SAVED = "live rows", "the save's rows", "saved rows"

# Several lines, so a Read with a limit of one shows only part of it.
READ_CONTENT = '{\n  "automations": [\n    {"name": "Morning brief", "status": "active"}\n  ]\n}\n'
MOVED_CONTENT = READ_CONTENT.replace("active", "paused")
REPORT = "Saved automations.json: 1 updated.\n- updated ...\nRead automations.json again before your next edit."
GONE = _Delete(automation_id="00000000-0000-4000-8000-000000000002", name="Evening wrap")


def _deleting(*gone: _Delete) -> Plan[FilePlan]:
    return Plan(FilePlan(deletes=list(gone)), [d.label for d in gone])


class _Conn:
    """The save's connection: records how its transaction ended."""

    def __init__(self) -> None:
        self.cursor_obj = MagicMock()
        self.cursor_obj.connection = self
        self.opened = False
        self.outcome: str | None = None

    @asynccontextmanager
    async def transaction(self):
        self.opened = True
        try:
            yield
        except BaseException:
            self.outcome = "rolled back"
            raise
        self.outcome = "committed"

    @asynccontextmanager
    async def cursor(self):
        yield self.cursor_obj


def _sandbox() -> MagicMock:
    sb = MagicMock()
    sb.normalize_path.side_effect = lambda p: p if p.startswith("/") else f"/home/workspace/{p}"
    return sb


@pytest.fixture
def backend() -> AutomationsBackend:
    return AutomationsBackend(user_id=USER, call=CALL, sandbox_backend=_sandbox(), root_prefix=PREFIX)


@pytest.fixture
def conn(monkeypatch) -> _Conn:
    fake = _Conn()

    @asynccontextmanager
    async def _connection():
        yield fake

    monkeypatch.setattr(db_json_route, "get_db_connection", _connection)
    return fake


@pytest.fixture
def file(monkeypatch, conn) -> MagicMock:
    """The file's steps: every read renders ``READ_CONTENT`` at ``v1`` until a
    test moves ``rendered``, the save's rows stand at ``v1``, and the plan
    deletes nothing unless a test says so."""
    fake = MagicMock(spec=AutomationsFile())
    fake.unchanged = None
    fake.rendered = {LIVE: (READ_CONTENT, "v1"), ROWS: (READ_CONTENT, "v1"), SAVED: (MOVED_CONTENT, "v2")}
    fake.fetch = AsyncMock(
        side_effect=lambda user_id, conn=None: LIVE if conn is None else SAVED if fake.commit.await_count else ROWS
    )
    fake.render = MagicMock(side_effect=lambda rows: fake.rendered[rows])
    fake.parse = AsyncMock(return_value=DOCUMENT)
    fake.plan = MagicMock(return_value=Plan(FilePlan()))
    fake.hold = AsyncMock(return_value=None)
    fake.commit = AsyncMock(return_value=REPORT)
    monkeypatch.setitem(AutomationsBackend.files, AUTOMATIONS_FILE, fake)
    return fake


async def _read(backend: AutomationsBackend, offset: int = 0, limit: int = 2000) -> str | None:
    """The Read tool's path through the route."""
    return await backend.aread_range(FILE_PATH, offset, limit)


class TestSave:
    @pytest.mark.asyncio
    async def test_a_save_plans_from_the_rows_it_checked_and_commits_on_their_connection(
        self, backend, file, conn
    ):
        await _read(backend)
        # Read inside, the user's settings would take a second pool connection
        # while the save's locks keep other saves waiting.
        opened_at_parse: list[bool] = []
        file.parse.side_effect = lambda *_: opened_at_parse.append(conn.opened) or DOCUMENT
        order: list[str] = []
        file.lock.side_effect = lambda *_: order.append("lock")
        file.plan.side_effect = lambda *_: order.append("plan") or Plan(FilePlan())

        await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        file.parse.assert_awaited_once_with(USER, CALL, MOVED_CONTENT, READ_CONTENT)
        assert opened_at_parse == [False]
        file.lock.assert_awaited_once_with(USER, conn)
        # The read, then the save's own read under the lock. Only a save
        # through the mount reads the file again as it left it.
        assert file.fetch.await_args_list == [call(USER), call(USER, conn)]
        assert order == ["lock", "plan"]
        file.plan.assert_called_once_with(CALL, DOCUMENT, ROWS)
        changes = file.hold.await_args.args[1]
        assert isinstance(changes, FilePlan)
        file.hold.assert_awaited_once_with(USER, changes, ROWS, conn)
        file.commit.assert_awaited_once_with(USER, changes, conn)
        file.committed.assert_awaited_once_with(USER, changes)
        assert conn.outcome == "committed"

    @pytest.mark.asyncio
    async def test_a_refused_plan_points_at_the_readme_and_commits_nothing(self, backend, file, conn):
        await _read(backend)
        file.plan.side_effect = AutomationFileError(
            error_type="schema_error", file=AUTOMATIONS_FILE, field_path="automations[0].name", hint="required"
        )

        with pytest.raises(AutomationFileError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert exc.value.message.endswith(f"See {README_PATH} for the fields and examples.")
        file.commit.assert_not_awaited()
        assert conn.outcome == "rolled back"

    @pytest.mark.asyncio
    async def test_content_that_is_no_document_is_refused_before_the_save_reads_anything(
        self, backend, file, conn
    ):
        await _read(backend)
        file.parse.side_effect = AutomationFileError(
            error_type="parse_error", file=AUTOMATIONS_FILE, field_path="", hint="invalid JSON"
        )

        with pytest.raises(AutomationFileError) as exc:
            await backend.awrite_text(FILE_PATH, "")

        assert exc.value.message.endswith(f"See {README_PATH} for the fields and examples.")
        file.lock.assert_not_awaited()
        assert not conn.opened

    @pytest.mark.asyncio
    async def test_a_refusal_while_committing_rolls_the_whole_save_back(self, backend, file, conn):
        """The lifecycle's refusals surface as the commit raising, after some
        rows may already have changed in the transaction."""
        await _read(backend)
        file.commit.side_effect = AutomationFileError(
            error_type="schema_error", file=AUTOMATIONS_FILE, field_path="", hint="x",
            problems=[("automations[0].llm_model", "unknown model")],
        )

        with pytest.raises(AutomationFileError):
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert conn.outcome == "rolled back"


class TestEdit:
    @pytest.mark.asyncio
    async def test_an_edit_lands_on_what_the_agent_last_read(self, backend, file):
        """Rows that moved after the Read are not what the agent edited. The
        Edit carries the Read's version, so the write conflicts instead of
        landing a change on content the agent never saw."""
        await _read(backend)
        file.rendered[LIVE] = (MOVED_CONTENT, "v2")

        result = await backend.aedit_text(FILE_PATH, '"Morning brief"', '"Evening brief"')

        assert result["success"] is True
        assert file.fetch.await_args_list.count(call(USER)) == 1
        _user_id, _call, content, served = file.parse.await_args.args
        assert content == READ_CONTENT.replace("Morning brief", "Evening brief")
        assert served == READ_CONTENT

    @pytest.mark.asyncio
    async def test_an_edit_reports_what_changed(self, backend, file):
        await _read(backend)

        result = await backend.aedit_text(FILE_PATH, '"active"', '"paused"')

        assert result["message"] == REPORT


class TestWrite:
    @pytest.mark.asyncio
    async def test_a_write_answers_with_the_report_and_spends_the_read(self, backend, file):
        await _read(backend)

        result = await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert result == {"success": True, "message": REPORT}
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)
        assert f"Read({FILE_PATH})" in exc.value.hint
        file.commit.assert_awaited_once()

    @pytest.mark.parametrize("via", ["write", "edit"])
    @pytest.mark.asyncio
    async def test_a_conflict_sends_the_agent_back_to_read(self, backend, file, conn, via):
        """Retrying over the same stale read would only conflict again."""
        await _read(backend)
        file.rendered[ROWS] = (MOVED_CONTENT, "v2")
        conflict = (
            f"{FILE_PATH} changed since your last Read, so this write could undo that change. "
            f"Read({FILE_PATH}) again and reapply your change."
        )

        if via == "write":
            with pytest.raises(UserDataValidationError) as exc:
                await backend.awrite_text(FILE_PATH, MOVED_CONTENT)
            assert (exc.value.error_type, exc.value.hint) == ("version_conflict", conflict)
        else:
            result = await backend.aedit_text(FILE_PATH, '"active"', '"paused"')
            assert result == {"success": False, "error": f"version_conflict:{AUTOMATIONS_FILE}: {conflict}"}

        file.plan.assert_not_called()
        assert conn.outcome == "rolled back"
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)
        assert exc.value.error_type == "read_required"
        assert f"Read({FILE_PATH})" in exc.value.hint
        file.commit.assert_not_awaited()


class TestWhatTheReadShowed:
    """A Write deletes whatever it leaves out, so it may delete only after a
    Read that showed every line."""

    @pytest.mark.parametrize(
        ("offset", "limit", "may_delete"),
        [(0, 2000, True), (0, 1, False), (1, 2000, False)],
        ids=["whole", "first-lines", "from-an-offset"],
    )
    @pytest.mark.asyncio
    async def test_a_write_may_delete_only_after_a_whole_read(self, backend, file, offset, limit, may_delete):
        await _read(backend, offset, limit)
        file.plan.return_value = _deleting(GONE)

        if may_delete:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)
            file.commit.assert_awaited_once()
            return
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)
        assert exc.value.error_type == "incomplete_read"
        assert exc.value.hint.startswith(f"this write leaves out {GONE.label}, which would delete it,")
        file.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_write_after_a_partial_read_that_deletes_nothing_goes_through(self, backend, file):
        await _read(backend, 0, 1)

        await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        file.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_refusal_lists_ten_deletes_and_counts_the_rest(self, backend, file):
        await _read(backend, 0, 1)
        gone = [_Delete(automation_id=f"id-{i}", name=f"Job {i}") for i in range(12)]
        file.plan.return_value = _deleting(*gone)

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert '"Job 9" (id-9) and 2 more, which would delete them' in exc.value.hint
        assert "Job 10" not in exc.value.hint

    @pytest.mark.asyncio
    async def test_a_read_clipped_for_length_is_not_whole(self, backend, file):
        file.rendered[LIVE] = ("x" * 200_000 + "\n", "v1")
        await _read(backend)
        file.plan.return_value = _deleting(GONE)

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert exc.value.error_type == "incomplete_read"

    @pytest.mark.asyncio
    async def test_an_edit_may_delete_what_it_quotes(self, backend, file):
        await _read(backend, 0, 1)
        file.plan.return_value = _deleting(GONE)

        result = await backend.aedit_text(FILE_PATH, '"active"', '"paused"')

        assert result["success"] is True
        file.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_edit_without_a_read_lands_on_the_live_file(self, backend, file):
        """old_string has to match the file, which is check enough; the load
        is not cached, so it can't stand in for a Read later."""
        file.rendered[LIVE] = file.rendered[ROWS] = (READ_CONTENT, "v2")

        result = await backend.aedit_text(FILE_PATH, '"active"', '"paused"')

        assert result["success"] is True
        assert file.parse.await_args.args[3] == READ_CONTENT
        assert backend._read_cache == {}

    @pytest.mark.parametrize("reader", ["grep", "aread_text"])
    @pytest.mark.asyncio
    async def test_only_a_read_lets_a_write_through(self, backend, file, reader):
        """A Grep, or the Edit tool reading the file back for the UI, shows the
        agent nothing it could base a whole-document Write on."""
        if reader == "grep":
            await backend.agrep_rich("Morning", PREFIX)
        else:
            await backend.aread_text(FILE_PATH)

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(FILE_PATH, MOVED_CONTENT)

        assert exc.value.error_type == "read_required"
        file.plan.assert_not_called()


class TestReadme:
    @pytest.mark.asyncio
    async def test_the_readme_is_readable_without_the_database(self, backend, file):
        content = await backend.aread_text(README_PATH)

        assert content.startswith("# Automations")
        file.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_readme_cannot_be_written_or_edited(self, backend, file):
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_text(README_PATH, "anything")
        edited = await backend.aedit_text(README_PATH, "# Automations", "# Mine")

        assert exc.value.error_type == "schema_error"
        assert "documentation" in exc.value.hint
        assert edited["success"] is False
        file.plan.assert_not_called()


class TestFileMount:
    """What a program reads and saves through the file mount. No Read tool call
    stands behind its save, so the route's version is the whole check, taken
    again under the route's lock."""

    @pytest.mark.asyncio
    async def test_a_mount_read_serves_the_routes_own_version(self, backend, file):
        assert await backend.aread_versioned(FILE_PATH) == (READ_CONTENT, "v1")
        assert await backend.aread_versioned(f"{PREFIX}notes.json") is None
        # A program's read is no Read a Write tool call can stand on.
        assert backend._read_cache == {}

    @pytest.mark.asyncio
    async def test_the_folder_lists_what_a_read_returns_and_only_the_data_file_as_writable(self, backend, file):
        # Non-ASCII on purpose: a size counted in characters would pass on ASCII.
        file.rendered[LIVE] = ('{"automations": [{"name": "Résumé 市场"}]}\n', "v1")

        entries = await backend.alist(PREFIX.rstrip("/"))

        assert [(e["name"], e["writable"]) for e in entries] == [(README_FILE, False), (AUTOMATIONS_FILE, True)]
        for entry in entries:
            content, version = await backend.aread_versioned(f"{PREFIX}{entry['name']}")
            assert (entry["size"], entry["version"]) == (len(content.encode()), version)
        assert await backend.alist(FILE_PATH) is None

    @pytest.mark.asyncio
    async def test_a_save_over_a_stale_version_conflicts_and_applies_nothing(self, backend, file):
        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_versioned(FILE_PATH, MOVED_CONTENT, "v0")

        assert exc.value.error_type == "version_conflict"
        file.plan.assert_not_called()
        file.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_save_over_the_live_version_may_delete_and_answers_with_the_report(
        self, backend, file, conn
    ):
        """A program writes the whole file, so what it leaves out is deleted,
        and the report lists every deletion. It read no Read tool's content,
        so the plan compares its state blocks with the live rows."""
        file.plan.return_value = _deleting(GONE)

        stored = await backend.awrite_versioned(FILE_PATH, MOVED_CONTENT, "v1")

        assert stored == (REPORT, MOVED_CONTENT, "v2")
        file.parse.assert_awaited_once_with(USER, CALL, MOVED_CONTENT, None)
        file.plan.assert_called_once_with(CALL, DOCUMENT, ROWS)
        file.commit.assert_awaited_once()
        # The file as the save left it, read in the save's own transaction.
        assert file.fetch.await_args_list == [call(USER, conn), call(USER, conn)]
        assert conn.outcome == "committed"

    @pytest.mark.asyncio
    async def test_a_save_that_changes_nothing_settles_on_the_rows_it_read(self, backend, file, conn):
        stored = await backend.awrite_versioned(FILE_PATH, MOVED_CONTENT, "v1")

        assert stored == (REPORT, READ_CONTENT, "v1")
        file.fetch.assert_awaited_once_with(USER, conn)

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_while_saving_is_a_server_error(self, backend, file, conn):
        file.commit.side_effect = RuntimeError("connection reset")

        with pytest.raises(UserDataValidationError) as exc:
            await backend.awrite_versioned(FILE_PATH, MOVED_CONTENT, "v1")

        assert exc.value.error_type == "server_error"
        assert "nothing was saved" in exc.value.hint
        assert conn.outcome == "rolled back"

    @pytest.mark.asyncio
    async def test_the_file_cannot_be_deleted_only_written_back_without_entries(self, backend, file):
        with pytest.raises(ReadOnlyStoreError) as exc:
            await backend.adelete_text(FILE_PATH)

        assert "entries removed" in str(exc.value)
        assert await backend.adelete_text(f"{PREFIX}notes.json") is False
        file.plan.assert_not_called()


class TestFilePanel:
    @pytest.mark.asyncio
    async def test_the_panel_loads_the_file_without_a_route_instance(self, file):
        content = await AutomationsBackend.load(f".agents/user/automations/{AUTOMATIONS_FILE}", USER)

        assert content == READ_CONTENT
        file.fetch.assert_awaited_once_with(USER)
