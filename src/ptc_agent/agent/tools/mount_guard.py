"""The store-backed path guard Bash and ExecuteCode share."""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox import livefs_mount

logger = structlog.get_logger(__name__)


async def run_guarded(
    backend: SandboxBackend,
    text: str,
    call_context: livefs_mount.CallContext | None,
    run: Callable[[str | None], Awaitable[tuple[str, dict[str, Any]]]],
    *,
    memory_error: str,
    automations_error: str,
    blocked_event: str,
    **blocked_fields: Any,
) -> tuple[str, dict[str, Any]]:
    """Refuse ``text`` if it names a store-backed tree no mount serves, else
    run it through the mount."""
    mount = await backend.settled_livefs(
        call_context.workspace_id if call_context is not None else None
    )
    tree = livefs_mount.unserved_tree(mount, text)
    if tree is not None:
        logger.info(blocked_event, tree=tree, **blocked_fields)
        error = memory_error if tree == "memory" else automations_error
        return error, {"mcp_trace": []}
    return await livefs_mount.through_mount(mount, call_context, run)
