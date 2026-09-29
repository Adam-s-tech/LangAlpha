"""What the mount serves of past threads: each workspace's transcripts and
the computer's thread index. Both are renders of server state, so both are
read-only, and neither is a filesystem route the file tools use: those reach
them through the sandbox paths the mount links in, so a search over hundreds
of megabytes runs in the sandbox rather than on the server.

Each is a ``MountRoute`` of its own, listing a directory from the stored
digests rather than every file's content.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime
from typing import Any

from ptc_agent.core.paths import THREAD_DIR_NAME, WorkspaceLayout
from ptc_agent.core.sandbox.livefs_runtime.protocol import Refusal
from src.server.services import transcripts
from src.server.services.livefs.routes import LivefsError, Saved, version_of
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

# A version's size never changes, so this only has to outlive the version.
_INDEX_SIZE_TTL_S = 86400


def _file(name: str, size: int, version: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "file",
        "size": size,
        "version": version,
        "writable": False,
    }


def _dir(name: str) -> dict[str, Any]:
    return {"name": name, "type": "dir"}


def _read_only(path: str) -> LivefsError:
    return LivefsError(Refusal.READ_ONLY, f"{path} is read only", path)


class _ReadOnly:
    root_prefix: str

    def is_writable(self, path: str) -> bool:
        return False

    def movable(self, path: str) -> bool:
        return False

    async def write(self, path: str, content: str, version: str | None) -> Saved:
        raise _read_only(path)

    async def delete(self, path: str) -> bool:
        raise _read_only(path)

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

    async def list(self, path: str) -> list[dict[str, Any]] | None:
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
                entries[name] = _file(name, size, sha256[:32])
        if not entries and inner:
            return None
        return sorted(entries.values(), key=lambda e: e["name"])

    async def read(self, path: str) -> tuple[str, str] | None:
        relative = self._relative(path)
        if not relative:
            return None
        short_id, _, inner = relative.partition("/")
        if not inner or not THREAD_DIR_NAME.match(short_id):
            return None
        found = await transcripts.read_file_with_digest(self._workspace_id, short_id, inner)
        if found is None:
            return None
        data, sha256 = found
        return data.decode(), sha256[:32]


@functools.cache
def _index_shape() -> str:
    """What the index makes of one fixed thread, which changes with its
    format: the daemon keeps a file's bytes by version for as long as it
    runs, past a deploy that renders the same threads differently."""
    at = datetime(2000, 1, 1, tzinfo=UTC)
    probe = {
        "conversation_thread_id": "00000000-0000-0000-0000-000000000000",
        "title": "t",
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "workspace_name": "w",
        "dir_name": "w",
        "created_at": at,
        "updated_at": at,
        "has_transcript": True,
    }
    return version_of(transcripts.index_content("/r", [probe]))


def _size_key(computer_id: str, version: str) -> str:
    return f"livefs:{{{computer_id}}}:index:{version}"


def _cache() -> Any:
    cache = get_cache_client()
    return cache.client if cache.enabled and cache.client else None


class IndexRoute(_ReadOnly):
    """The computer's thread index, generated from the database per read.

    Its version comes from a digest of every field it shows, which the
    database computes without sending the threads, so a listing costs one
    small query rather than the whole render. The size needs the render, so
    it is kept by version, which fixes the bytes; a listing that finds none
    renders the file and carries its content, since a read usually follows.
    """

    def __init__(self, computer_id: str, root: str) -> None:
        self._computer_id = computer_id
        self._root = root
        self._path = transcripts.index_path(root)
        self.root_prefix = self._path.rsplit("/", 1)[0] + "/"

    def _version(self, digest: str) -> str:
        return version_of(f"{_index_shape()}\n{self._root}\n{digest}")

    async def _render(self) -> tuple[str, str, int]:
        """The index, its version and size, from one snapshot of the threads."""
        from src.server.database.conversation import list_computer_threads

        digest, rows = await list_computer_threads(self._computer_id)
        text = transcripts.index_content(self._root, rows)
        version, size = self._version(digest), len(text.encode())
        cache = _cache()
        if cache is not None:
            try:
                await cache.set(
                    _size_key(self._computer_id, version), size, ex=_INDEX_SIZE_TTL_S
                )
            except Exception:
                logger.debug("livefs index size not kept", exc_info=True)
        return text, version, size

    async def _known(self, cache: Any) -> tuple[str, int] | None:
        """The current version and its size, when a render already found it."""
        from src.server.database.conversation import list_computer_threads

        digest, _ = await list_computer_threads(self._computer_id, rows=False)
        version = self._version(digest)
        try:
            size = await cache.get(_size_key(self._computer_id, version))
        except Exception:
            logger.debug("livefs index size not read", exc_info=True)
            return None
        return (version, int(size)) if size is not None else None

    async def list(self, path: str) -> list[dict[str, Any]] | None:
        if self._relative(path) != "":
            return None
        cache = _cache()
        known = await self._known(cache) if cache is not None else None
        if known is not None:
            return [_file(transcripts.INDEX, known[1], known[0])]
        text, version, size = await self._render()
        return [{**_file(transcripts.INDEX, size, version), "content": text}]

    async def read(self, path: str) -> tuple[str, str] | None:
        if path != self._path:
            return None
        text, version, _ = await self._render()
        return text, version
