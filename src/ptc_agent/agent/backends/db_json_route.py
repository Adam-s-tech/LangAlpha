"""DbJsonRoute: a mounted directory of JSON files that are rows in Postgres.

The shared plumbing behind ``UserDataBackend`` (`.agents/user/profile/`) and
``AutomationsBackend`` (`.agents/user/automations/`). Reads render live rows.
Every save, from the Write and Edit tools or through the file mount, runs the
same flow: check the content for everything the rows don't decide, then under
the user's lock check the version the writer last saw, plan the write from the
rows that check read, refuse a plan that deletes what the writer never saw,
and commit, all in one transaction. A subclass names its directory, its README
and a ``DbJsonFile`` for each file, which supplies those steps for its rows.

The version (a hash of the agent-visible content) never reaches the agent. A
Read caches it beside the content it served, so a Write is refused unless the
agent read the file in this request and nobody changed it since. An Edit is
checked against the content it edited: the last Read's, or the live file's
when there was none, since ``old_string`` has to match it either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, NamedTuple

import structlog

from ptc_agent.agent.backends.langgraph_store import ReadOnlyStoreError, grep_texts, lock_for_namespace
from ptc_agent.agent.backends.read_window import shows_whole_file
from ptc_agent.agent.backends.results import EditTextResult, WriteTextResult
from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.server.database.pool import get_db_connection

logger = structlog.get_logger(__name__)

README_FILE = "README.md"

# Past this many, a refusal counts the rest rather than naming them.
_LISTED_DELETES = 10


ErrorType = Literal[
    "parse_error",
    "schema_error",
    "version_conflict",
    "read_required",
    "incomplete_read",
    "constraint_error",
    "server_error",
]


@dataclass
class UserDataValidationError(Exception):
    """Raised when a write payload fails parse / schema / version / constraint checks.

    The backend's `awrite_text` surfaces the `message` verbatim to the agent's
    Write/Edit tool, so it must be self-explanatory and tell the agent how to recover.
    """

    error_type: ErrorType
    file: str  # e.g. "portfolio.json"
    field_path: str  # e.g. "holdings[2].quantity"
    hint: str
    # The README beside the file, which the route that refused the write
    # names, since only it knows where the file is mounted.
    readme: str | None = None

    def _text(self) -> str:
        where = f"{self.file}:{self.field_path}" if self.field_path else self.file
        return f"{self.error_type}:{where}: {self.hint}"

    @property
    def message(self) -> str:
        text = self._text()
        # Parse + schema failures usually mean the agent guessed at the shape,
        # so the next retry reads the documented one. Not for README.md
        # itself, where the pointer would lead back to the refused file.
        if self.readme and self.error_type in {"parse_error", "schema_error"} and self.file != "README.md":
            separator = "\n" if "\n" in text else " "
            text += f"{separator}See {self.readme} for the fields and examples."
        return text

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Served:
    """A file as a write is checked against it."""

    content: str
    version: str
    # The agent was shown every line of ``content``. Only a Read can say so.
    whole: bool = False


@dataclass
class Plan[C]:
    """What one save changes. ``deletes`` names each row it removes, which a
    Write after a Read that showed only part of the file may not do."""

    changes: C
    deletes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)


class Stored(NamedTuple):
    """What a save left: the writer's report, and for a writer that holds the
    file next, the file as the save left it and its version."""

    report: str | None
    content: str | None = None
    version: str | None = None


# A failure unrelated to the content. A model told only to retry kept retrying,
# then created throwaway entries in the user's data to find the cause.
_SERVER_FAILURE = (
    "the server failed while saving, for a reason unrelated to your content; nothing was saved. "
    "Retry once. If it fails again, stop and tell the user; don't add test entries to diagnose it."
)


@functools.cache
def _text_version(text: str) -> str:
    """A README's version, hashed once per class rather than per listing."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class DbJsonFile[R, P, C]:
    """One file of rows: ``R`` the rows, ``P`` a written file as parsed, ``C``
    what a save of it changes. The route runs each save through these in one
    order, under its locks; a file only says what its rows are."""

    # The report of a write of the very content the writer read.
    unchanged: ClassVar[str | None] = None

    async def fetch(self, user_id: str, conn: Any = None) -> R:
        """The rows; during a save, on ``conn``'s transaction."""
        raise NotImplementedError

    def render(self, rows: R) -> tuple[str, str]:
        """The file as the agent reads it, and its version."""
        raise NotImplementedError

    async def lock(self, user_id: str, conn: Any) -> None:
        """Serialize this user's writers across processes, for the rest of
        ``conn``'s transaction."""
        raise NotImplementedError

    async def parse(self, user_id: str, call: CallContext, content: str, served: str | None) -> P:
        """``content`` checked for everything the rows don't decide, with
        whatever the plan reads beyond the rows, before the save locks
        anything: content refused here costs no query, and a read here holds
        no second pool connection while the save's locks keep other saves
        waiting. ``served`` is the content the writer saw, or None when it
        saw the live file. Raises ``UserDataValidationError`` on content it
        refuses."""
        raise NotImplementedError

    def plan(self, call: CallContext, parsed: P, rows: R) -> Plan[C]:
        """What writing ``parsed`` over ``rows`` changes. Raises
        ``UserDataValidationError`` on content it refuses."""
        raise NotImplementedError

    async def hold(self, user_id: str, changes: C, rows: R, conn: Any) -> str | None:
        """Lock what ``changes`` writes over, for the rest of ``conn``'s
        transaction, and the version of ``rows`` as they stand locked, which
        the save holds to the one it checked. None when the save's own lock
        already keeps every other writer out."""
        return None

    async def commit(self, user_id: str, changes: C, conn: Any) -> str | None:
        """Write ``changes``; the report the writer reads, or None for a
        plain acknowledgement. Raising rolls the whole save back."""
        raise NotImplementedError

    async def committed(self, user_id: str, changes: C) -> None:
        """After the commit: drop what caches the rows outside the route."""


class DbJsonRoute:
    """Composite route over a fixed set of DB-backed JSON files plus a README."""

    # The directory under the sandbox root, a key of ``USER_DATA_FILES``.
    directory: ClassVar[str] = ""
    files: ClassVar[Mapping[str, DbJsonFile[Any, Any, Any]]] = {}
    data_files: ClassVar[frozenset[str]] = frozenset()
    readme_content: ClassVar[str] = ""

    # How the file panel serves these files: read-only, since a write there
    # would skip the checks this route makes.
    source: ClassVar[str] = ""
    read_failure: ClassVar[str] = ""
    read_only: ClassVar[str] = ""
    undeletable: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.data_files = frozenset(cls.files)

    def __init__(
        self,
        *,
        user_id: str,
        call: CallContext,
        sandbox_backend: SandboxBackend,
        root_prefix: str,
    ) -> None:
        if not root_prefix.endswith("/"):
            root_prefix = root_prefix + "/"
        self._user_id = user_id
        # Who a save runs for, which a file whose meaning depends on the
        # conversation reads its defaults from.
        self._call = call
        self._sandbox = sandbox_backend
        self._root_prefix = root_prefix
        # Filename → what the agent's last Read served. Only the Read tool's
        # path fills it. Request-scoped via agent.py, never reused across users,
        # but shared by the subagents of the request.
        self._read_cache: dict[str, Served] = {}

    # --- file panel ---

    @classmethod
    async def load(cls, path: str, user_id: str) -> str:
        """The file at ``path`` (relative to the sandbox root), as the agent
        reads it."""
        file = cls.files[path.rsplit("/", 1)[-1]]
        content, _version = file.render(await file.fetch(user_id))
        return content

    # --- composite-compatible surface ---

    @property
    def root_prefix(self) -> str:
        return self._root_prefix

    def normalize_path(self, path: str) -> str:
        return self._sandbox.normalize_path(path)

    def virtualize_path(self, path: str) -> str:
        return self._sandbox.virtualize_path(path)

    def validate_path(self, path: str) -> bool:
        return self._sandbox.validate_path(path)

    @property
    def filesystem_config(self) -> Any:
        return self._sandbox.filesystem_config

    # --- helpers ---

    @property
    def _known_files(self) -> frozenset[str]:
        return self.data_files | {README_FILE}

    def _namespace(self) -> tuple[str, ...]:
        """Key of the in-process lock serializing this user's writes."""
        return (self._user_id, self.directory)

    def _filename(self, normalized_path: str) -> str | None:
        """Return the known basename if `path` is one of this route's files; else None."""
        if not normalized_path.startswith(self._root_prefix):
            return None
        suffix = normalized_path[len(self._root_prefix):]
        if "/" in suffix:
            return None
        if suffix in self._known_files:
            return suffix
        return None

    def _absolute(self, filename: str) -> str:
        return f"{self._root_prefix}{filename}"

    def _refusal(self, error_type: ErrorType, filename: str, hint: str) -> UserDataValidationError:
        return UserDataValidationError(
            error_type=error_type,
            file=filename,
            field_path="",
            hint=hint,
            readme=self._absolute(README_FILE),
        )

    def _readme_refusal(self, file_path: str) -> UserDataValidationError:
        names = " / ".join(sorted(self.data_files))
        return self._refusal(
            "schema_error",
            README_FILE,
            f"{file_path} is documentation, not data; it can't be edited. Update {names} instead.",
        )

    def exists(self, file_path: str) -> bool:
        """Whether a file is at ``file_path``: each of this route's always is."""
        return self._filename(file_path) is not None

    async def _live(self, filename: str) -> Served:
        file = self.files[filename]
        return Served(*file.render(await file.fetch(self._user_id)))

    def _invalidate(self, filename: str) -> None:
        self._read_cache.pop(filename, None)

    def _require_read(self, filename: str) -> Served:
        """What the agent last read, or a refusal telling it to read."""
        served = self._read_cache.get(filename)
        if served is None:
            path = self._absolute(filename)
            # A reply to a question starts a new run with an empty cache, which
            # the model sees as the same turn, so say what reset it.
            raise self._refusal(
                "read_required",
                filename,
                f"no Read of {path} since this run started (answering a question starts a new "
                f"run). Read({path}), then write again.",
            )
        return served

    def _unseen_deletes(self, filename: str, deletes: list[str]) -> UserDataValidationError:
        listed = ", ".join(deletes[:_LISTED_DELETES])
        if len(deletes) > _LISTED_DELETES:
            listed += f" and {len(deletes) - _LISTED_DELETES} more"
        return self._refusal(
            "incomplete_read",
            filename,
            f"this write leaves out {listed}, which would delete "
            f"{'it' if len(deletes) == 1 else 'them'}, but your last Read didn't show the whole "
            "file. Read it without offset or limit before a Write that removes anything. If it "
            "is too long to show whole, remove each one with Edit.",
        )

    @contextlib.contextmanager
    def _refusals(self, filename: str) -> Iterator[None]:
        """Answer every failure of a save that saved nothing as a refusal."""
        try:
            yield
        except UserDataValidationError as exc:
            # A conflict drops the cached Read so the next one pulls fresh
            # rows; kept, a retry would conflict again.
            if exc.error_type == "version_conflict":
                self._invalidate(filename)
            exc.readme = exc.readme or self._absolute(README_FILE)
            raise
        except Exception as exc:
            logger.exception("db json route write failed", path=self._absolute(filename))
            raise self._refusal("server_error", filename, _SERVER_FAILURE) from exc

    async def _save(
        self,
        filename: str,
        content: str,
        version: str,
        *,
        served: str | None,
        may_delete: bool,
        settle: bool = False,
    ) -> Stored:
        """Plan and commit a save over ``version``, under the in-process and
        database locks. ``may_delete`` says the writer saw everything the save
        could drop: a Write after a Read of the whole file, an Edit, which
        removes only text it quotes, or a program, which writes the whole file.
        ``settle`` answers with the file as the save left it, read in the
        save's own transaction, which spares the writer reading it back.
        """
        file = self.files[filename]
        if served is not None and content == served:
            # Written back as it was read: nothing to change, whatever the
            # rows did since, so nothing to read or lock.
            self._invalidate(filename)
            return Stored(file.unchanged)
        path = self._absolute(filename)
        conflict = self._refusal(
            "version_conflict",
            filename,
            f"{path} changed since your last Read, so this write could undo that change. "
            f"Read({path}) again and reapply your change.",
        )
        user_id = self._user_id
        with self._refusals(filename):
            parsed = await file.parse(user_id, self._call, content, served)
        async with lock_for_namespace(self._namespace()):
            with self._refusals(filename):
                # One transaction, which anything raised inside rolls back:
                # the plan and the writes go over the rows the version check
                # read, under the lock taken before reading them.
                async with get_db_connection() as conn, conn.transaction():
                    await file.lock(user_id, conn)
                    rows = await file.fetch(user_id, conn)
                    if file.render(rows)[1] != version:
                        raise conflict
                    plan = file.plan(self._call, parsed, rows)
                    if plan.deletes and not may_delete:
                        raise self._unseen_deletes(filename, plan.deletes)
                    held = await file.hold(user_id, plan.changes, rows, conn)
                    if held is not None and held != version:
                        raise conflict
                    report = await file.commit(user_id, plan.changes, conn)
                    if settle:
                        # A save that changed nothing leaves the rows it read.
                        after = file.render(await file.fetch(user_id, conn) if plan else rows)
            await file.committed(user_id, plan.changes)
            self._invalidate(filename)
        return Stored(report, *after) if settle else Stored(report)

    # --- read ---

    async def aread_text(self, file_path: str) -> str | None:
        """The live file, for readers other than the Read tool.

        It leaves the read cache alone: the agent never saw this content, so it
        can't vouch for a later Write.
        """
        filename = self._filename(file_path)
        if filename is None:
            # Anything under the prefix that isn't a known file is not ours:
            # return None so the composite reports file-not-found.
            return None
        if filename == README_FILE:
            return self.readme_content
        try:
            return (await self._live(filename)).content
        except Exception:
            logger.exception("db json route read failed", path=file_path)
            return None

    async def aread_range(self, file_path: str, offset: int = 0, limit: int = 2000) -> str | None:
        """The Read tool's path. Every Read shows live rows, even ones changed
        earlier in the turn, and records what it showed for the next Write."""
        filename = self._filename(file_path)
        if filename is None:
            return None
        if filename == README_FILE:
            content = self.readme_content
        else:
            try:
                live = await self._live(filename)
            except Exception:
                logger.exception("db json route read failed", path=file_path)
                return None
            content = live.content
            self._read_cache[filename] = Served(
                content, live.version, whole=shows_whole_file(content, offset, limit)
            )
        lines = content.splitlines(keepends=True)
        start = max(0, offset)
        end = start + max(0, limit)
        return "".join(lines[start:end])

    # --- write / edit ---

    def _unwritable(self, file_path: str, filename: str | None) -> UserDataValidationError:
        if filename == README_FILE:
            return self._readme_refusal(file_path)
        # The composite routed this path here, so the folder is ours and the
        # file can't exist; say which ones do rather than just fail.
        names = " / ".join(sorted(self.data_files))
        return self._refusal(
            "schema_error",
            file_path.rsplit("/", 1)[-1],
            f"{file_path} can't be created: this folder holds only {names} and {README_FILE}, "
            f"which the server keeps. Update {names} instead.",
        )

    async def awrite_text(self, file_path: str, content: str) -> bool | WriteTextResult:
        """Validate + apply a JSON write. Raises UserDataValidationError on bad input."""
        filename = self._filename(file_path)
        if filename is None or filename == README_FILE:
            raise self._unwritable(file_path, filename)

        base = self._require_read(filename)
        report = (
            await self._save(filename, content, base.version, served=base.content, may_delete=base.whole)
        ).report
        if report:
            return {"success": True, "message": report}
        return True

    async def aedit_text(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> EditTextResult:
        filename = self._filename(file_path)
        if filename is None:
            return {"success": False, "error": f"File not found: {file_path}"}
        if filename == README_FILE:
            return {"success": False, "error": self._readme_refusal(file_path).hint}
        if old_string == new_string:
            return {"success": False, "error": "old_string and new_string are identical"}

        base = self._read_cache.get(filename)
        if base is None:
            try:
                base = await self._live(filename)
            except Exception as exc:
                return {"success": False, "error": f"Read failed: {exc!s}"}
        content = base.content

        occurrences = content.count(old_string)
        if occurrences == 0:
            preview = old_string if len(old_string) <= 120 else f"{old_string[:120]}…"
            return {"success": False, "error": f"String not found: {preview!r}"}
        if occurrences > 1 and not replace_all:
            return {
                "success": False,
                "error": (
                    f"String appears {occurrences} times. Provide more context or "
                    "set replace_all=True."
                ),
            }
        new_content = content.replace(old_string, new_string, occurrences if replace_all else 1)

        try:
            report = (
                await self._save(filename, new_content, base.version, served=base.content, may_delete=True)
            ).report
        except UserDataValidationError as exc:
            return {"success": False, "error": str(exc)}

        if report:
            message = report
        elif replace_all:
            message = f"Edited {file_path} ({occurrences} occurrences replaced)"
        else:
            message = f"Edited {file_path}"
        return {
            "success": True,
            "occurrences": occurrences if replace_all else 1,
            "size": len(new_content),
            "message": message,
        }

    # --- file mount ---
    #
    # The mount checks a save against the version it served, so it serves this
    # route's version rather than a hash of the content: a run moving `state`
    # changes the content but must not make a save conflict. No Read tool call
    # stands behind a save there, so the version is the whole check.

    def is_writable(self, file_path: str) -> bool:
        return self._filename(file_path) in self.data_files

    async def _versioned(self, filename: str) -> tuple[str, str]:
        if filename == README_FILE:
            return self.readme_content, _text_version(self.readme_content)
        live = await self._live(filename)
        return live.content, live.version

    async def aread_versioned(self, file_path: str) -> tuple[str, str] | None:
        """(content, version), or None for a path this route has no file at.
        Raises when the rows can't be read, which the mount answers as a retry."""
        filename = self._filename(file_path)
        if filename is None:
            return None
        return await self._versioned(filename)

    async def alist(self, path: str) -> list[dict[str, Any]] | None:
        """Every file, rendered for its size, so each carries the content it
        was rendered from and a read after the listing costs nothing."""
        if path.rstrip("/") != self._root_prefix.rstrip("/"):
            return None
        names = sorted(self._known_files)
        served = await asyncio.gather(*(self._versioned(name) for name in names))
        return [
            {
                "name": name,
                "type": "file",
                "size": len(content.encode()),
                "version": version,
                "writable": name in self.data_files,
                "content": content,
            }
            for name, (content, version) in zip(names, served)
        ]

    async def awrite_versioned(self, file_path: str, content: str, version: str) -> Stored:
        """Apply a save through the mount over ``version``: its report, and
        the file as it left it with its version, which the mount holds next.

        A program writes the whole file, so the save may delete what it leaves
        out, and its report lists every deletion. Raises
        ``UserDataValidationError`` as ``awrite_text`` does.
        """
        filename = self._filename(file_path)
        if filename is None or filename == README_FILE:
            raise self._unwritable(file_path, filename)
        return await self._save(filename, content, version, served=None, may_delete=True, settle=True)

    async def adelete_text(self, file_path: str) -> bool:
        if self._filename(file_path) is None:
            return False
        raise ReadOnlyStoreError(
            f"{file_path} is a view of saved data and cannot be deleted. "
            "Write it back with the entries removed to clear it."
        )

    # --- glob / grep ---

    def _covers(self, normalized_path: str) -> bool:
        return normalized_path.startswith(self._root_prefix) or (
            normalized_path.rstrip("/") == self._root_prefix.rstrip("/")
        )

    async def aglob_paths(self, pattern: str, path: str = ".") -> list[str]:
        if not self._covers(self.normalize_path(path)):
            return []
        out: list[str] = []
        for filename in sorted(self._known_files):
            absolute = self._absolute(filename)
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(absolute, pattern):
                out.append(absolute)
        return out

    async def agrep_rich(
        self,
        pattern: str,
        path: str = ".",
        output_mode: str = "files_with_matches",
        glob: str | None = None,
        type: str | None = None,  # noqa: A002 - mirror sandbox.agrep_rich
        *,
        case_insensitive: bool = False,
        show_line_numbers: bool = True,
        lines_after: int | None = None,
        lines_before: int | None = None,
        lines_context: int | None = None,
        multiline: bool = False,
        head_limit: int | None = None,
        offset: int = 0,
    ) -> Any:
        # Context-window args (lines_after / lines_before / lines_context / multiline)
        # are accepted for signature parity with the sandbox grep but are not
        # honored: these files are small JSON documents the agent should
        # just Read whole instead of grep-context-paging.
        if not self._covers(self.normalize_path(path)):
            return []

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags=flags)
        except re.error:
            return []

        texts: list[tuple[str, str]] = []
        for filename in sorted(self._known_files):
            absolute = self._absolute(filename)
            if glob and not fnmatch.fnmatch(filename, glob) and not fnmatch.fnmatch(absolute, glob):
                continue
            try:
                if filename == README_FILE:
                    content = self.readme_content
                else:
                    # What the agent last read, else the live file. Never
                    # cached: a Grep is no Read a Write can stand on.
                    served = self._read_cache.get(filename)
                    content = (served or await self._live(filename)).content
            except Exception:
                continue
            texts.append((absolute, content))
        return grep_texts(
            texts, compiled, output_mode, show_line_numbers=show_line_numbers, head_limit=head_limit, offset=offset
        )
