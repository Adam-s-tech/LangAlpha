"""A workspace folder is its name, so a glob rooted in one reads the folder literally."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ptc_agent.core.sandbox.files import aglob_files


class _LocalRuntime:
    """Runs the shipped glob script with this interpreter."""

    async def exec(self, command: str, timeout: int = 60):
        command = command.replace("python3", sys.executable, 1)
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(
            stdout=done.stdout, stderr=done.stderr, exit_code=done.returncode
        )


def _sandbox() -> SimpleNamespace:
    async def _call(fn, *args, retry_policy=None, **kwargs):
        return await fn(*args, **kwargs)

    return SimpleNamespace(
        _wait_ready=AsyncMock(),
        config=SimpleNamespace(
            filesystem=SimpleNamespace(enable_path_validation=False)
        ),
        _normalize_search_path=lambda path: path,
        runtime=_LocalRuntime(),
        _runtime_call=_call,
    )


@pytest.mark.asyncio
async def test_a_folder_named_with_glob_characters_is_searched_literally(tmp_path):
    folder = tmp_path / "Q3 [draft] *?"
    (folder / "results").mkdir(parents=True)
    (folder / "results" / "report.md").write_text("x")

    matches = await aglob_files(_sandbox(), "results/*.md", str(folder))

    assert matches == [str(folder / "results" / "report.md")]
