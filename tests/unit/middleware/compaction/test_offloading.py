"""Tests for the compaction offloading helpers.

Focuses on the `overwrite=True` contract with `SandboxBackend.awrite` —
a regression dropping that kwarg would break compaction mid-conversation
once a message id or tool_call_id is retried (since protocol-default
`awrite` is create-only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.agent.middleware.compaction.offloading import aoffload_truncated_args


def _make_backend(*, error: str | None = None) -> AsyncMock:
    backend = AsyncMock()
    result = MagicMock()
    result.error = error
    result.path = None if error else "/some/path"
    backend.awrite = AsyncMock(return_value=result)
    return backend


class TestAoffloadTruncatedArgs:
    @pytest.mark.asyncio
    async def test_passes_overwrite_true(self):
        backend = _make_backend()
        originals = {"call1": {"name": "Search", "args": {"query": "x"}}}
        with patch(
            "ptc_agent.agent.middleware.compaction.offloading.get_thread_id",
            return_value="t",
        ):
            await aoffload_truncated_args(backend, originals)
        assert backend.awrite.call_count == 1
        assert backend.awrite.call_args.kwargs.get("overwrite") is True

    @pytest.mark.asyncio
    async def test_noop_when_backend_none(self):
        await aoffload_truncated_args(None, {"c1": {"name": "X", "args": {}}})

    @pytest.mark.asyncio
    async def test_noop_when_no_originals(self):
        backend = _make_backend()
        await aoffload_truncated_args(backend, {})
        backend.awrite.assert_not_called()

    @pytest.mark.asyncio
    async def test_path_includes_tool_call_id(self):
        backend = _make_backend()
        with patch(
            "ptc_agent.agent.middleware.compaction.offloading.get_thread_id",
            return_value="t",
        ):
            await aoffload_truncated_args(
                backend, {"xyz789": {"name": "Search", "args": {}}}
            )
        path = backend.awrite.call_args.args[0]
        assert "truncated_args_xyz789.md" in path

    @pytest.mark.asyncio
    async def test_error_is_logged_not_raised(self):
        """Errors from backend.awrite must be swallowed."""
        backend = _make_backend(error="disk full")
        with patch(
            "ptc_agent.agent.middleware.compaction.offloading.get_thread_id",
            return_value="t",
        ):
            # Must not raise
            await aoffload_truncated_args(
                backend, {"c1": {"name": "X", "args": {"k": "v"}}}
            )
