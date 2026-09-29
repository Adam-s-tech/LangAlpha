"""What the mount does at one route: list a directory, read, save and delete a file.

The store's routes answer the file tools under two contracts. A plain store
route keeps the bytes it is sent and has no versions of its own, so a file's
version is its content's hash and a save is checked against a read first. A
``DbJsonRoute`` renders database rows, versions them itself and checks a save
against that version under its write lock. Each kind is adapted here once,
so the tree asks every route the same questions (``MountRoute``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple, Protocol

from ptc_agent.agent.backends import (
    InvalidStoreKeyError,
    ReadOnlyStoreError,
    StoreBackend,
    StoreContentTooLargeError,
    WorkflowsBackend,
)
from ptc_agent.agent.backends.db_json_route import DbJsonRoute
from ptc_agent.agent.backends.langgraph_store import StoreListingIncomplete
from ptc_agent.core.sandbox.livefs_runtime.protocol import Refusal
from src.server.services.user_data_io import UserDataValidationError

logger = logging.getLogger(__name__)

_STATUS = {
    Refusal.NOT_FOUND: 404,
    Refusal.IS_DIRECTORY: 409,
    Refusal.NOT_DIRECTORY: 409,
    Refusal.EXISTS: 412,
    Refusal.CHANGED: 412,
    Refusal.READ_ONLY: 403,
    Refusal.INVALID: 422,
    Refusal.PRECONDITION_REQUIRED: 428,
    Refusal.TOO_LARGE: 413,
    Refusal.UNAVAILABLE: 503,
}


class LivefsError(Exception):
    """A refusal the daemon turns into an errno and the agent reads as text.
    ``path`` is the sandbox path the agent used, which its outcome is filed by."""

    def __init__(self, code: Refusal, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = _STATUS[code]
        self.message = message
        self.path = path


def not_found(path: str) -> LivefsError:
    return LivefsError(Refusal.NOT_FOUND, f"No such file or directory: {path}", path)


def changed(path: str) -> LivefsError:
    return LivefsError(
        Refusal.CHANGED,
        f"{path} changed since it was read. Read it again and reapply the change.",
        path,
    )


def version_of(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def file_entry(name: str, content: str, *, writable: bool) -> dict[str, Any]:
    """A listing entry for a file whose content the route holds, carried
    along so the daemon need not read it (the tree trims what it may carry)."""
    data = content.encode()
    return {
        "name": name,
        "type": "file",
        "size": len(data),
        "version": hashlib.sha256(data).hexdigest()[:32],
        "writable": writable,
        "content": content,
    }


def is_root(root_prefix: str, path: str) -> bool:
    return path.rstrip("/") == root_prefix.rstrip("/")


class Saved(NamedTuple):
    version: str
    size: int
    #: The sandbox path the agent knows the file by.
    path: str
    #: What the save changed, for a file that reports that.
    report: str | None
    #: Whether the file now holds exactly the bytes sent, so the sender may
    #: keep them as its content at ``version`` rather than read it back.
    as_sent: bool


class MountRoute(Protocol):
    """What the tree asks of a route, at sandbox paths under ``root_prefix``."""

    @property
    def root_prefix(self) -> str: ...

    def is_writable(self, path: str) -> bool: ...

    def movable(self, path: str) -> bool:
        """Whether a rename may take the file away from here."""

    async def list(self, path: str) -> list[dict[str, Any]] | None:
        """The directory's entries; None where there is no directory. A file's
        ``version`` is the one ``read`` returns with the same bytes, and it may
        carry ``content`` when the route holds it anyway."""

    async def read(self, path: str) -> tuple[str, str] | None:
        """Content and the version a save must name; None where no file is."""

    async def write(self, path: str, content: str, version: str | None) -> Saved:
        """Save over ``version``, or only where no file is when it is None."""

    async def delete(self, path: str) -> bool:
        """Whether there was a file to delete."""


@contextmanager
def _refusing(path: str) -> Iterator[None]:
    """A route's refusal of a change, as the refusal the daemon answers."""
    try:
        yield
    except ReadOnlyStoreError as exc:
        raise LivefsError(Refusal.READ_ONLY, str(exc), path) from exc
    except StoreContentTooLargeError as exc:
        raise LivefsError(Refusal.TOO_LARGE, str(exc), path) from exc
    except UserDataValidationError as exc:
        if exc.error_type == "version_conflict":
            raise changed(path) from exc
        if exc.error_type == "server_error":
            raise LivefsError(Refusal.UNAVAILABLE, exc.hint, path) from exc
        raise LivefsError(Refusal.INVALID, exc.message, path) from exc
    except (InvalidStoreKeyError, ValueError) as exc:
        raise LivefsError(Refusal.INVALID, str(exc), path) from exc


class _StoreRoute:
    """Files kept as sent. A directory is listed from one read of the subtree,
    which holds every file's content, so the listing carries it."""

    def __init__(self, route: StoreBackend | WorkflowsBackend) -> None:
        self._route = route
        self.root_prefix = route.root_prefix

    def is_writable(self, path: str) -> bool:
        return self._route.is_writable(path)

    def movable(self, path: str) -> bool:
        return self._route.is_writable(path)

    async def list(self, path: str) -> list[dict[str, Any]] | None:
        try:
            tree = await self._route.aread_tree(path)
        except StoreListingIncomplete as exc:
            raise LivefsError(Refusal.UNAVAILABLE, str(exc), path) from exc
        if path in tree:
            raise LivefsError(Refusal.NOT_DIRECTORY, f"{path} is a file", path)
        base = path.rstrip("/") + "/"
        entries: dict[str, dict[str, Any]] = {}
        for file_path, content in tree.items():
            if not file_path.startswith(base):
                continue
            name, nested, _ = file_path[len(base) :].partition("/")
            if nested:
                entries.setdefault(name, {"name": name, "type": "dir"})
                continue
            entries[name] = file_entry(
                name, content, writable=self._route.is_writable(file_path)
            )
        if not entries and not is_root(self.root_prefix, path):
            return None
        return sorted(entries.values(), key=lambda e: e["name"])

    async def read(self, path: str) -> tuple[str, str] | None:
        content = await self._route.aread_text(path)
        return None if content is None else (content, version_of(content))

    async def write(self, path: str, content: str, version: str | None) -> Saved:
        # The check and the write are two steps: a writer landing between
        # them in the same moment is not caught, as with the file tools.
        current = await self.read(path)
        if version is None:
            if current is not None:
                raise LivefsError(Refusal.EXISTS, f"{path} already exists", path)
        elif current is None or current[1] != version:
            raise changed(path)
        with _refusing(path):
            ok = await self._route.awrite_text(path, content)
        if not ok:
            raise LivefsError(Refusal.UNAVAILABLE, f"{path} was not saved; retry", path)
        return Saved(version_of(content), len(content.encode()), path, None, True)

    async def delete(self, path: str) -> bool:
        with _refusing(path):
            try:
                return await self._route.adelete_text(path)
            except asyncio.TimeoutError as exc:
                raise LivefsError(
                    Refusal.UNAVAILABLE, f"{path} was not deleted; retry", path
                ) from exc


class _RowsRoute:
    """Rows rendered as a file, which a save replaces and nothing deletes.

    A save goes over the version the sender names, which the route checks
    under its write lock. It stores the rows, not the bytes, so the save
    answers with the file as its own transaction left it, which is the
    version and content the sender holds next.
    """

    def __init__(self, route: DbJsonRoute) -> None:
        self._route = route
        self.root_prefix = route.root_prefix

    def is_writable(self, path: str) -> bool:
        return self._route.is_writable(path)

    def movable(self, path: str) -> bool:
        return False

    async def list(self, path: str) -> list[dict[str, Any]] | None:
        return await self._route.alist(path)

    async def read(self, path: str) -> tuple[str, str] | None:
        return await self._route.aread_versioned(path)

    async def write(self, path: str, content: str, version: str | None) -> Saved:
        if version is None and await self._route.aread_versioned(path) is not None:
            raise LivefsError(Refusal.EXISTS, f"{path} already exists", path)
        with _refusing(path):
            stored = await self._route.awrite_versioned(path, content, version or "")
        final = stored.content
        return Saved(stored.version, len(final.encode()), path, stored.report, final == content)

    async def delete(self, path: str) -> bool:
        with _refusing(path):
            return await self._route.adelete_text(path)


def adapt(route: Any) -> MountRoute:
    """The mount's view of a route the file tools' filesystem resolved."""
    if isinstance(route, DbJsonRoute):
        return _RowsRoute(route)
    return _StoreRoute(route)
