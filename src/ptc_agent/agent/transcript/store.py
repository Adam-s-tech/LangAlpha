"""Write rendered segments into a transcript directory, touching only what changed.

A manifest beside the segment files records each one's digest, so a re-export
after a turn rewrites that turn's file and the manifest, not the whole thread.
Anything the manifest no longer lists (a turn dropped by a branch rewind) is
removed. The directory is a cache of the checkpoint: deleting it loses nothing,
the next export rebuilds it.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AnyMessage

from ptc_agent.agent.transcript.render import SCHEMA_VERSION, Segment, render
from ptc_agent.core.paths import WorkspaceLayout

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"
TASK_META = "meta.json"
TRANSCRIPT_DIR = "transcript"
TASKS_DIR = "tasks"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def transcript_subdir(short_thread_id: str) -> str:
    """A thread's transcript directory, workspace-relative."""
    return WorkspaceLayout.thread_subdir(short_thread_id, TRANSCRIPT_DIR)


def task_subdir(short_thread_id: str, task_id: str) -> str:
    """One background task's transcript directory, workspace-relative."""
    name = _UNSAFE.sub("_", task_id) or "_"
    return f"{transcript_subdir(short_thread_id)}/{TASKS_DIR}/{name}"


@dataclass(frozen=True)
class TranscriptTarget:
    """Where one agent's transcript lives: a thread's turns or a task's runs."""

    directory: str
    unit: str = "turn"
    manifest: str = MANIFEST

    @classmethod
    def for_agent(cls, thread_id: str, checkpoint_ns: str = "") -> TranscriptTarget:
        """Resolve from graph config: a subagent's namespace opens with ``task:<id>``."""
        head = checkpoint_ns.split("|", 1)[0]
        if head.startswith("task:"):
            return cls(task_subdir(thread_id[:8], head[len("task:") :]), "run", TASK_META)
        return cls(transcript_subdir(thread_id[:8]))


class TranscriptIO(Protocol):
    async def read_text(self, path: str) -> str | None: ...

    async def write_files(self, files: dict[str, str]) -> None: ...

    async def remove(self, paths: list[str]) -> None: ...


def segment_file(unit: str, number: int) -> str:
    return f"{unit}-{number:04d}.jsonl"


def load_manifest(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def plan_writes(
    directory: str,
    segments: list[Segment],
    *,
    previous: dict[str, Any],
    unit: str = "turn",
    manifest_name: str = MANIFEST,
    header: dict[str, Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Files to write and files to remove to bring ``directory`` in line.

    Pure, so one export can batch several directories into a single upload.
    """
    known = {
        entry.get("file"): entry.get("sha1")
        for entry in previous.get("segments", [])
        if isinstance(entry, dict)
    }

    entries = []
    writes: dict[str, str] = {}
    for segment in segments:
        name = segment_file(unit, segment.number)
        digest = segment.digest
        entries.append(
            {
                "file": name,
                unit: segment.number,
                "at": segment.at,
                "events": len(segment.lines),
                "first_id": segment.first_id,
                "last_id": segment.last_id,
                "sha1": digest,
            }
        )
        if known.get(name) != digest:
            writes[f"{directory}/{name}"] = segment.content

    manifest = {"schema": SCHEMA_VERSION, **(header or {}), "segments": entries}
    if manifest != previous:
        writes[f"{directory}/{manifest_name}"] = json.dumps(
            manifest, ensure_ascii=False, indent=1, default=str
        )

    current = {entry["file"] for entry in entries}
    stale = [f"{directory}/{name}" for name in known if name and name not in current]
    return writes, stale


async def write_segments(
    io: TranscriptIO,
    directory: str,
    segments: list[Segment],
    *,
    unit: str = "turn",
    manifest_name: str = MANIFEST,
    header: dict[str, Any] | None = None,
) -> int:
    """Bring ``directory`` in line with ``segments``; returns files written."""
    previous = load_manifest(await io.read_text(f"{directory}/{manifest_name}"))
    writes, stale = plan_writes(
        directory,
        segments,
        previous=previous,
        unit=unit,
        manifest_name=manifest_name,
        header=header,
    )
    if writes:
        await io.write_files(writes)
    if stale:
        await io.remove(stale)
    return len(writes)


class BackendIO:
    """TranscriptIO over the agent's sandbox backend.

    Paths are resolved to absolute ones up front, so the ``rm`` lands where
    the upload did rather than under the shell's working directory.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def read_text(self, path: str) -> str | None:
        return await self._backend.aread_text(self._backend.normalize_path(path))

    async def write_files(self, files: dict[str, str]) -> None:
        results = await self._backend.aupload_files(
            [
                (self._backend.normalize_path(path), content.encode())
                for path, content in files.items()
            ]
        )
        failed = [r.path for r in results if r.error]
        if failed:
            raise OSError(f"transcript upload failed: {failed}")

    async def remove(self, paths: list[str]) -> None:
        await self._backend.aexecute(
            "rm -f -- "
            + " ".join(shlex.quote(self._backend.normalize_path(p)) for p in paths)
        )


async def export_live(
    io: TranscriptIO, target: TranscriptTarget, messages: list[AnyMessage]
) -> int:
    """Write a transcript from messages in hand, mid-turn, ahead of the server.

    Compaction points the model at the transcript while the turn in progress
    has not been exported yet. The manifest keeps the server's header but loses
    its fingerprint, so the next server export re-renders from the checkpoint
    rather than trusting this one. Segments land before the manifest: a
    manifest naming a digest its file does not have would stop the server from
    ever rewriting that file.
    """
    manifest_path = f"{target.directory}/{target.manifest}"
    previous = load_manifest(await io.read_text(manifest_path))
    header = {k: v for k, v in previous.items() if k not in ("schema", "segments")}
    segments = render(messages, unit=target.unit)

    def plan() -> tuple[dict[str, str], list[str]]:
        return plan_writes(
            target.directory,
            segments,
            previous=previous,
            unit=target.unit,
            manifest_name=target.manifest,
            header=header,
        )

    writes, stale = plan()
    if not stale and set(writes) <= {manifest_path}:
        return 0
    if "fingerprint" in header:
        header["fingerprint"] = None
        writes, stale = plan()

    manifest = writes.pop(manifest_path, None)
    if writes:
        await io.write_files(writes)
    if stale:
        await io.remove(stale)
    if manifest is not None:
        await io.write_files({manifest_path: manifest})
    return len(writes) + (manifest is not None)
