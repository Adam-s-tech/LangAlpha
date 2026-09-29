"""What the mount serves of past threads: each workspace's transcripts and
the computer's thread index. Both are renders of server state, so both are
read-only, and neither is a filesystem route the file tools use: those reach
them through the sandbox paths the mount links in, so a search over hundreds
of megabytes runs in the sandbox rather than on the server.

A route here lists a directory itself (``alist``) instead of handing the tree
every file's content to derive the listing from.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ptc_agent.agent.backends import ReadOnlyStoreError
from ptc_agent.core.paths import THREAD_DIR_NAME, WorkspaceLayout
from src.server.services import transcripts


def _file(name: str, size: int, sha256: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "file",
        "size": size,
        "version": sha256[:32],
        "writable": False,
    }


def _dir(name: str) -> dict[str, Any]:
    return {"name": name, "type": "dir"}


class _ReadOnly:
    root_prefix: str

    def is_writable(self, path: str) -> bool:
        return False

    async def awrite_text(self, path: str, content: str) -> bool:
        raise ReadOnlyStoreError(f"{path} is read only")

    async def adelete_text(self, path: str) -> bool:
        raise ReadOnlyStoreError(f"{path} is read only")

    def _relative(self, path: str) -> str | None:
        base = self.root_prefix.rstrip("/")
        if path.rstrip("/") == base:
            return ""
        if path.startswith(base + "/"):
            return path[len(base) + 1 :].strip("/")
        return None


class TranscriptRoute(_ReadOnly):
    """One workspace's stored transcripts, a directory per thread."""

    def __init__(self, workspace_id: str, layout: WorkspaceLayout) -> None:
        self._workspace_id = workspace_id
        self.root_prefix = layout.transcripts + "/"

    async def alist(self, path: str) -> list[dict[str, Any]] | None:
        from src.server.database import thread_transcripts as db

        relative = self._relative(path)
        if relative is None:
            return None
        if not relative:
            ids = await db.workspace_transcripts(self._workspace_id)
            return [_dir(name) for name in sorted({t[:8] for t in ids})]
        short_id, _, inner = relative.partition("/")
        if not THREAD_DIR_NAME.match(short_id):
            return None
        files = await transcripts.list_files(self._workspace_id, short_id)
        if files is None:
            return None
        base = f"{inner}/" if inner else ""
        entries: dict[str, dict[str, Any]] = {}
        for file_path, (sha256, size) in files.items():
            if not file_path.startswith(base):
                continue
            name, nested, _ = file_path[len(base) :].partition("/")
            if nested:
                entries.setdefault(name, _dir(name))
            else:
                entries[name] = _file(name, size, sha256)
        if not entries and inner:
            return None
        return sorted(entries.values(), key=lambda e: e["name"])

    async def aread_text(self, path: str) -> str | None:
        relative = self._relative(path)
        if not relative:
            return None
        short_id, _, inner = relative.partition("/")
        if not inner or not THREAD_DIR_NAME.match(short_id):
            return None
        data = await transcripts.read_file(self._workspace_id, short_id, inner)
        return data.decode(errors="replace") if data is not None else None


class IndexRoute(_ReadOnly):
    """The computer's thread index, generated from the database per read."""

    def __init__(self, computer_id: str, root: str) -> None:
        self._computer_id = computer_id
        self._root = root
        self._path = transcripts.index_path(root)
        self.root_prefix = self._path.rsplit("/", 1)[0] + "/"

    async def _content(self) -> str:
        from src.server.database.conversation import list_computer_threads

        return transcripts.index_content(
            self._root, await list_computer_threads(self._computer_id)
        )

    async def alist(self, path: str) -> list[dict[str, Any]] | None:
        if self._relative(path) != "":
            return None
        data = (await self._content()).encode()
        return [_file(transcripts.INDEX, len(data), hashlib.sha256(data).hexdigest())]

    async def aread_text(self, path: str) -> str | None:
        return await self._content() if path == self._path else None
