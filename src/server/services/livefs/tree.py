"""Mount paths resolved onto the store routes the file tools already use.

The mount shows the user's files at paths of its own (``user/memory/...``,
``workflows/...``, ``workspaces/<id>/memory/...``), which the sandbox
links in where the file tools show the same files. Past threads
(``workspaces/<id>/transcripts/...``, ``computer/threads.jsonl``) are the
exception: read-only renders the file tools reach only through the links. Each request builds
the routes the agent's filesystem gets, over a stand-in sandbox that only
answers path questions, so the mount and the tools agree on what is
readable, writable and valid by construction, and a path no route owns has
no files behind it to serve.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

from ptc_agent.agent.backends import (
    CompositeFilesystemBackend,
    InvalidStoreKeyError,
    ReadOnlyStoreError,
    StoreContentTooLargeError,
)
from ptc_agent.agent.backends.langgraph_store import StoreListingIncomplete
from ptc_agent.agent.filesystem_routes import (
    build_filesystem_backend,
    resolve_identity_gates,
)
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
from src.server.database import workspace as workspace_db
from src.server.services.livefs.history import IndexRoute, TranscriptRoute
from src.server.services.livefs.tokens import LivefsIdentity
from src.server.services.user_data_io import UserDataValidationError

USER_TIERS = ("memory", "memo", "profile")


class LivefsError(Exception):
    """A refusal the daemon turns into an errno and the agent reads as text."""

    def __init__(self, status: int, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.path = path


def version_of(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def _not_found(path: str) -> LivefsError:
    return LivefsError(404, "not_found", f"No such file or directory: {path}", path)


def _is_directory(path: str) -> LivefsError:
    return LivefsError(409, "is_directory", f"{path} is a directory", path)


class _PathOnlySandbox:
    """What the store routes ask of a sandbox: path shape, never files."""

    filesystem_config = None

    def __init__(self, root: str) -> None:
        self.computer_root = root

    def normalize_path(self, path: str) -> str:
        return path

    def virtualize_path(self, path: str) -> str:
        return path

    def validate_path(self, path: str) -> bool:
        return True


@dataclass(frozen=True)
class _Node:
    """A virtual directory (``children``) or a path a route answers for."""

    children: tuple[str, ...] | None = None
    route: Any = None
    path: str = ""

    @property
    def is_route_root(self) -> bool:
        return self.path.rstrip("/") == self.route.root_prefix.rstrip("/")


def _segments(path: str) -> tuple[str, ...]:
    parts = tuple(p for p in path.strip("/").split("/") if p)
    if any(p in (".", "..") for p in parts):
        raise LivefsError(422, "invalid", f"Invalid path: {path}", path)
    return parts


class LivefsTree:
    """One request's view of a computer's mount. Holds no state past it."""

    def __init__(self, identity: LivefsIdentity, store: Any) -> None:
        self._identity = identity
        self._store = store
        self._layout = SandboxLayout.for_root(identity.root_dir)
        self._root = self._layout.root

    def _backend(
        self, workspace_id: str | None, layout: WorkspaceLayout | None
    ) -> CompositeFilesystemBackend | None:
        user_id = self._identity.user_id
        gates = resolve_identity_gates(
            store=self._store,
            user_id=user_id,
            workspace_id=workspace_id,
            disable_subagents=True,
        )
        backend, _ = build_filesystem_backend(
            backend=_PathOnlySandbox(self._root),
            gates=gates,
            store=self._store,
            user_id=user_id,
            workspace_id=workspace_id,
            layout=layout,
        )
        return backend if isinstance(backend, CompositeFilesystemBackend) else None

    def _routed(self, backend: CompositeFilesystemBackend | None, path: str) -> _Node:
        route = backend.route_for(path) if backend is not None else None
        if route is None:
            raise _not_found(path)
        return _Node(route=route, path=path)

    async def _resolve(self, parts: tuple[str, ...]) -> _Node:
        if not parts:
            computer = self._backend(None, None)
            names = ["computer", "workspaces"]
            if computer is not None and computer.route_for(self._layout.workflows):
                names.insert(0, "workflows")
            if computer is not None and any(
                computer.route_for(self._layout.join(SandboxLayout.USER_DIR, t))
                for t in USER_TIERS
            ):
                names.insert(0, "user")
            return _Node(children=tuple(names))
        head, rest = parts[0], parts[1:]
        if head == "user":
            computer = self._backend(None, None)
            if not rest:
                return _Node(
                    children=tuple(
                        t
                        for t in USER_TIERS
                        if computer is not None
                        and computer.route_for(
                            self._layout.join(SandboxLayout.USER_DIR, t)
                        )
                    )
                )
            return self._routed(
                computer, self._layout.join(SandboxLayout.USER_DIR, *rest)
            )
        if head == "computer":
            route = IndexRoute(self._identity.computer_id, self._root)
            return _Node(
                route=route, path=self._layout.join(SandboxLayout.AGENTS_DIR, *rest)
            )
        if head == "workflows":
            return self._routed(
                self._backend(None, None),
                self._layout.join(SandboxLayout.WORKFLOWS_DIR, *rest),
            )
        if head == "workspaces":
            if not rest:
                folders = await workspace_db.get_live_workspace_folders_for_computer(
                    self._identity.computer_id
                )
                return _Node(children=tuple(str(f["workspace_id"]) for f in folders))
            workspace = await workspace_db.get_workspace(rest[0])
            # A workspace on another computer has no folder here to link or
            # name, and separate computers are the isolation boundary.
            if (
                workspace is None
                or workspace.get("user_id") != self._identity.user_id
                or str(workspace.get("computer_id")) != self._identity.computer_id
            ):
                raise _not_found("/".join(parts))
            layout = self._layout.for_workspace(workspace.get("dir_name"))
            backend = self._backend(str(workspace["workspace_id"]), layout)
            if len(rest) == 1:
                has_memory = backend is not None and backend.route_for(layout.memory)
                return _Node(
                    children=("memory", "transcripts") if has_memory else ("transcripts",)
                )
            if rest[1] == "transcripts":
                route = TranscriptRoute(str(workspace["workspace_id"]), layout)
                return _Node(route=route, path="/".join((layout.transcripts, *rest[2:])))
            if rest[1] != "memory":
                raise _not_found("/".join(parts))
            return self._routed(backend, "/".join((layout.memory, *rest[2:])))
        raise _not_found("/".join(parts))

    async def _is_dir(self, node: _Node) -> bool:
        if node.is_route_root:
            return True
        if hasattr(node.route, "alist"):
            return await node.route.alist(node.path) is not None
        return bool(await node.route.aread_tree(node.path))

    async def _file(self, path: str) -> _Node:
        node = await self._resolve(_segments(path))
        if node.children is not None or node.is_route_root:
            raise _is_directory(node.path or path)
        return node

    async def list(self, path: str) -> tuple[list[dict[str, Any]], bool]:
        """The directory's entries, and whether new files can be created in it."""
        node = await self._resolve(_segments(path))
        if node.children is not None:
            return [{"name": name, "type": "dir"} for name in node.children], False
        if hasattr(node.route, "alist"):
            listed = await node.route.alist(node.path)
            if listed is None:
                raise _not_found(node.path)
            return listed, node.route.is_writable(node.path)
        try:
            tree = await node.route.aread_tree(node.path)
        except StoreListingIncomplete as exc:
            raise LivefsError(503, "unavailable", str(exc), node.path) from exc
        if node.path in tree:
            raise LivefsError(409, "not_directory", f"{node.path} is a file", node.path)
        base = node.path.rstrip("/") + "/"
        entries: dict[str, dict[str, Any]] = {}
        for file_path, content in tree.items():
            if not file_path.startswith(base):
                continue
            name, nested, _ = file_path[len(base):].partition("/")
            if nested:
                entries.setdefault(name, {"name": name, "type": "dir"})
                continue
            entries[name] = {
                "name": name,
                "type": "file",
                "size": len(content.encode()),
                "version": version_of(content),
                "writable": node.route.is_writable(file_path),
            }
        if not entries and not node.is_route_root:
            raise _not_found(node.path)
        return (
            sorted(entries.values(), key=lambda e: e["name"]),
            node.route.is_writable(node.path),
        )

    async def read(self, path: str) -> tuple[str, str, str]:
        """Content, version and the sandbox path the agent knows it by."""
        node = await self._file(path)
        content = await node.route.aread_text(node.path)
        if content is None:
            if await self._is_dir(node):
                raise _is_directory(node.path)
            raise _not_found(node.path)
        return content, version_of(content), node.path

    async def write(
        self,
        path: str,
        data: bytes,
        *,
        if_match: str | None,
        if_none_match: str | None,
    ) -> tuple[str, int, str]:
        """Replace a whole file on the precondition the caller saw.

        Every write names what it replaces, the version it read or that
        there was none, so a save never lands over a change the caller did
        not see. The check and the write are two steps: a writer landing
        between them in the same moment is not caught, as with the tools.
        """
        node = await self._file(path)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise LivefsError(
                422, "invalid", f"{node.path}: these files hold UTF-8 text", node.path
            ) from None
        # Also primes the read a profile write checks its version against.
        current = await node.route.aread_text(node.path)
        if if_none_match == "*":
            if current is not None:
                raise LivefsError(412, "exists", f"{node.path} already exists", node.path)
        elif if_match is not None:
            if current is None or version_of(current) != if_match.strip('"'):
                raise LivefsError(
                    412,
                    "changed",
                    f"{node.path} changed since it was read. Read it again and "
                    "reapply the change.",
                    node.path,
                )
        else:
            raise LivefsError(
                428, "precondition_required", "Send If-Match or If-None-Match", node.path
            )
        await self._write(node, content)
        stored = await node.route.aread_text(node.path)
        final = stored if stored is not None else content
        return version_of(final), len(final.encode()), node.path

    async def delete(self, path: str) -> str:
        node = await self._file(path)
        if not await self._delete(node):
            if await self._is_dir(node):
                raise _is_directory(node.path)
            raise _not_found(node.path)
        return node.path

    async def rename(self, path: str, to: str) -> tuple[str, str]:
        """Move a file as write-then-delete, so a failure leaves the source.

        Directories are refused as ``is_directory``; the daemon answers that
        as a cross-device rename, which ``mv`` completes file by file.
        """
        source = await self._file(path)
        target = await self._file(to)
        if source.path == target.path:
            return source.path, target.path
        content = await source.route.aread_text(source.path)
        if content is None:
            if await self._is_dir(source):
                raise _is_directory(source.path)
            raise _not_found(source.path)
        if not source.route.is_writable(source.path):
            raise LivefsError(
                403, "read_only", f"{source.path} cannot be moved", source.path
            )
        await target.route.aread_text(target.path)
        await self._write(target, content)
        await self._delete(source)
        return source.path, target.path

    async def _write(self, node: _Node, content: str) -> None:
        try:
            ok = await node.route.awrite_text(node.path, content)
        except ReadOnlyStoreError as exc:
            raise LivefsError(403, "read_only", str(exc), node.path) from exc
        except StoreContentTooLargeError as exc:
            raise LivefsError(413, "too_large", str(exc), node.path) from exc
        except UserDataValidationError as exc:
            status = 412 if exc.error_type == "version_conflict" else 422
            code = "changed" if status == 412 else "invalid"
            raise LivefsError(status, code, exc.message, node.path) from exc
        except (InvalidStoreKeyError, ValueError) as exc:
            raise LivefsError(422, "invalid", str(exc), node.path) from exc
        if not ok:
            raise LivefsError(
                503, "unavailable", f"{node.path} was not saved; retry", node.path
            )

    async def _delete(self, node: _Node) -> bool:
        try:
            return await node.route.adelete_text(node.path)
        except ReadOnlyStoreError as exc:
            raise LivefsError(403, "read_only", str(exc), node.path) from exc
        except InvalidStoreKeyError as exc:
            raise LivefsError(422, "invalid", str(exc), node.path) from exc
        except asyncio.TimeoutError as exc:
            raise LivefsError(
                503, "unavailable", f"{node.path} was not deleted; retry", node.path
            ) from exc
