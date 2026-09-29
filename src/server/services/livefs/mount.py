"""Keep a computer's file mount serving, with a token that has time left.

Every caller wants the same end state: the mount up, linked into exactly the
computer's live folders, and a token that is not running low. So bring-up, a
joining workspace and a warm turn all call ``ensure``, and the daemon does
whichever part is missing. Failures are logged and reported, never raised:
without the mount the file tools still reach these files through the store,
and code that needs them is refused instead of writing beside them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import livefs_mount
from src.server.database import livefs_tokens as db
from src.server.database import workspace as workspace_db
from src.server.database.workspace_folders import is_top_level, workspace_folders_lock
from src.server.services import transcripts
from src.server.services.egress.reachability import effective_relay_base_url
from src.server.services.livefs import outcomes, tokens
from src.server.services.livefs.tree import LivefsTree

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountState:
    mounted: bool
    #: When the token the sandbox now holds runs out; None when unknown.
    expires_at: datetime | None = None
    reason: str | None = None
    #: The workspaces whose folders are linked in.
    workspace_ids: frozenset[str] = frozenset()
    #: The daemon's error code when not mounted; ``installing`` clears soon.
    error: str | None = None
    #: (mount path, sandbox path) for each link.
    links: tuple[tuple[str, str], ...] = ()
    #: This call started the daemon, fresh or in place of a dead one.
    started: bool = False


#: strerror(ENOTCONN), what every path through a dead FUSE mount answers.
_DEAD_MOUNT = "Transport endpoint is not connected"


def runs_low(expires_at: datetime | None) -> bool:
    return expires_at is None or expires_at - datetime.now(UTC) < tokens.REFRESH_BELOW


async def _links(
    computer_id: str, user_id: str, layout: SandboxLayout
) -> tuple[list[tuple[str, str]], frozenset[str]]:
    """Mount paths and the sandbox paths the file tools show the same files
    at, and the workspaces those include."""
    from src.server.app import setup

    identity = tokens.LivefsIdentity(computer_id, user_id, layout.root)
    top, _ = await LivefsTree(identity, setup.store).list("")
    names = {entry["name"] for entry in top}
    links: list[tuple[str, str]] = []
    workspace_ids: set[str] = set()
    if "user" in names:
        links.append(("user", layout.join(SandboxLayout.USER_DIR)))
    if "workflows" in names:
        links.append(("workflows", layout.workflows))
    for folder in await workspace_db.get_live_workspace_folders_for_computer(
        computer_id
    ):
        dir_name = folder.get("dir_name")
        if dir_name and not is_top_level(dir_name):
            # Staged mid-move: a link there would make the staging folder
            # the move treats as the content. It is linked once it lands.
            continue
        workspace_id = str(folder["workspace_id"])
        workspace = layout.for_workspace(dir_name)
        if setup.store is not None:
            links.append((f"workspaces/{workspace_id}/memory", workspace.memory))
        links.append((f"workspaces/{workspace_id}/transcripts", workspace.transcripts))
        workspace_ids.add(workspace_id)
    if links:
        links.append(("computer/" + transcripts.INDEX, transcripts.index_path(layout.root)))
    return links, frozenset(workspace_ids)


def _off(computer_id: str, reason: str) -> MountState:
    logger.info("livefs off for computer %s: %s", computer_id, reason)
    return MountState(False, reason=reason)


async def ensure(
    sandbox: Any,
    *,
    computer_id: str,
    user_id: str | None,
    provider: str,
    fresh_token: bool = False,
) -> MountState:
    """Bring the mount to its wanted state; ``fresh_token`` rewrites the
    token even when the current one has time left, as bring-up does so a
    changed server address reaches the sandbox."""
    computer_id = str(computer_id)
    if sandbox is None or getattr(sandbox, "runtime", None) is None or not user_id:
        return _off(computer_id, "no sandbox or owner")
    base_url = effective_relay_base_url(provider)
    try:
        # The links go into every folder on the computer, so no settle may
        # move one between reading where it is and linking into it: a stale
        # read recreates the folder that moved, or points a sibling that took
        # its name at the other workspace's files.
        async with workspace_folders_lock(computer_id) as folders, db.publish_lock(
            computer_id
        ):
            if folders is None:
                return MountState(False, reason="folders are moving", error="busy")
            links, workspace_ids = await _links(computer_id, user_id, sandbox.layout)
            if not links:
                return _off(computer_id, "nothing to mount")
            expires_at = await db.token_expiry(computer_id)
            config = None
            if fresh_token or runs_low(expires_at):
                minted = await tokens.mint_token(computer_id, user_id)
                config = {"base_url": base_url, "token": minted.token}
                expires_at = minted.expires_at
            # The index a sync used to write sits where its link goes.
            replace = (transcripts.index_path(sandbox.layout.root),)
            outcome = await livefs_mount.up(
                sandbox, config=config, links=links, replace=replace
            )
            if outcome.error == "no_config" and config is None:
                # A sandbox rebuilt under a live token has no copy of it.
                minted = await tokens.mint_token(computer_id, user_id)
                config = {"base_url": base_url, "token": minted.token}
                expires_at = minted.expires_at
                outcome = await livefs_mount.up(
                    sandbox, config=config, links=links, replace=replace
                )
    except Exception as exc:
        logger.warning(
            "livefs mount failed for computer %s: %s", computer_id, exc, exc_info=True
        )
        return MountState(False, reason=str(exc))
    if not outcome.ok:
        logger.warning(
            "livefs mount not up for computer %s: %s %s %s",
            computer_id,
            outcome.error,
            outcome.reason or "",
            outcome.failed or "",
        )
        return MountState(
            False, expires_at, outcome.reason or outcome.error, error=outcome.error
        )
    if outcome.set_aside:
        logger.warning(
            "livefs moved files that sat where its links go on computer %s: %s",
            computer_id,
            outcome.set_aside,
        )
    if outcome.started:
        logger.info("livefs mounted on computer %s (%d links)", computer_id, len(links))
    return MountState(
        True,
        expires_at,
        workspace_ids=workspace_ids,
        links=tuple(links),
        started=bool(outcome.started),
    )


class Handle:
    """The serving mount as the file tools see it (``MountHandle``), on the
    worker running the turn. Execution context only: ``refresh`` goes through
    ``ensure``, whose lock and token table are the truth."""

    def __init__(
        self,
        computer_id: str,
        state: MountState,
        refresh: Callable[[], Awaitable[MountState]],
    ) -> None:
        self._computer_id = computer_id
        self._expires_at = state.expires_at
        self._links = state.links
        self._refresh = refresh
        self._lock = asyncio.Lock()

    async def prepare(self) -> None:
        # Parallel commands (subagents) share one refresh.
        async with self._lock:
            if runs_low(self._expires_at):
                state = await self._refresh()
                self._expires_at = state.expires_at or self._expires_at

    async def report(self, call_id: str, output: str) -> str:
        text = outcomes.describe(
            await outcomes.collect(self._computer_id, call_id), self._links
        )
        # A killed daemon leaves the links answering ENOTCONN until something
        # runs ``up``, and a warm turn does not, so the command that meets it
        # does.
        if _DEAD_MOUNT in output:
            async with self._lock:
                state = await self._refresh()
            if state.started:
                note = "The file mount had stopped and is serving again; rerun the command."
                text = f"{text}\n\n{note}" if text else note
        return text

    async def save_transcript(self, target: Any, messages: list[Any]) -> bool:
        return await transcripts.save_live(target, messages)


async def revoke(computer_id: Any) -> None:
    """End every token the computer holds; a stopped or deleted one serves nothing."""
    try:
        await tokens.revoke(str(computer_id))
    except Exception:
        logger.warning("livefs token not revoked for computer %s", computer_id, exc_info=True)
