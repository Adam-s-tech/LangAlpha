"""Keep a computer's file mount serving, with a token that has time left,
and linked into exactly its live folders.

The two are asked of the sandbox apart. ``serve`` installs the token and
starts the daemon; it needs no folder list, so a bring-up runs it beside its
asset sync, and a token gone or a daemon found dead asks it alone. ``link``
lays the links, which point through the mount's own link and so hold across
a restart: it is asked once the mount serves and whenever a folder joins.
A token running low is replaced by ``renew`` in the background. Failures
are logged and reported, never raised: without the mount the file tools
still reach these files through the store, and code that needs them is
refused instead of writing beside them.
"""

from __future__ import annotations

import logging
import posixpath
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from langchain_core.messages import AnyMessage

from ptc_agent.agent.transcript import TranscriptTarget
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import livefs_mount
from ptc_agent.core.sandbox.livefs_mount import CallContext, MountOutcome
from ptc_agent.core.sandbox.livefs_runtime.protocol import GENERATIONS, MOUNT, MountError
from src.server.database import livefs_tokens as db
from src.server.database.workspace_folders import workspace_folders_lock
from src.server.services import transcripts
from src.server.services.egress.reachability import effective_relay_base_url
from src.server.services.livefs import outcomes, tokens
from src.server.services.livefs.tokens import lapsed, runs_low
from src.server.services.livefs.tracker import MountTracker, sandbox_id
from src.server.services.livefs.tree import LivefsTree

logger = logging.getLogger(__name__)

#: How long a turn waits on the folders lock before going on without the
#: mount work. A settle holds it for as long as its sandbox script runs, and
#: the turn can run on whatever the sandbox has meanwhile.
TURN_LOCK_WAIT_S = 1.5

#: What a second ``serve`` right after would answer again; the rest (code a
#: sync was still replacing, a settle holding the folders, no clear answer)
#: may clear while a bring-up's asset sync runs.
_SETTLED = frozenset(
    {
        MountError.UNSUPPORTED,
        MountError.INSTALLING,
        MountError.UNREACHABLE,
        MountError.BAD_CONFIG,
    }
)
#: What the daemon answers before a staged token is installed; after any
#: other answer the sandbox holds the token, whatever else held the mount back.
#: ``stale_code`` comes from ``boot``, before any of the daemon's code runs.
_NOT_INSTALLED = frozenset(
    {
        MountError.UNSUPPORTED,
        MountError.BAD_CONFIG,
        MountError.NO_CONFIG,
        MountError.UNANSWERED,
        MountError.STALE_CODE,
    }
)


@dataclass(frozen=True)
class MountState:
    """What ``serve`` answered."""

    mounted: bool
    #: When the token the sandbox now holds runs out; None when unknown.
    expires_at: datetime | None = None
    reason: str | None = None
    #: The daemon's error code when not mounted; ``installing`` clears soon.
    error: MountError | None = None
    #: This call started the daemon, fresh or in place of a dead one.
    started: bool = False

    @property
    def settled(self) -> bool:
        """Whether asking again at once would answer the same."""
        return self.mounted or self.error in _SETTLED


@dataclass(frozen=True)
class LinkState:
    """What ``link`` answered, by folder: a workspace, or None for the
    computer's own links."""

    #: The folders whose links all went in; None when the daemon did not
    #: answer for the list, which leaves what was linked before as it was.
    linked: frozenset[str | None] | None = None
    #: The folders the list held whose links did not all go in.
    failed: frozenset[str | None] = frozenset()
    error: MountError | None = None
    reason: str | None = None


#: strerror(ENOTCONN), what every path through a dead FUSE mount answers.
_DEAD_MOUNT = "Transport endpoint is not connected"
#: A socket answers ENOTCONN too, so the error counts only beside a path the
#: mount serves: the mount itself, a generation of it, or a link under
#: ``.agents``.
_MOUNTED_PATHS = (MOUNT, GENERATIONS, f"{SandboxLayout.AGENTS_DIR}/")


def _met_dead_mount(output: str) -> bool:
    return any(
        _DEAD_MOUNT in line and any(path in line for path in _MOUNTED_PATHS)
        for line in output.splitlines()
    )


def _off(computer_id: str, reason: str) -> MountState:
    logger.info("livefs off for computer %s: %s", computer_id, reason)
    return MountState(False, reason=reason)


def _usable(sandbox: Any, user_id: str | None) -> bool:
    return (
        sandbox is not None
        and getattr(sandbox, "runtime", None) is not None
        and bool(user_id)
    )


def _config(base_url: str, minted: tokens.MintedToken) -> dict[str, Any]:
    return {"base_url": base_url, "token": minted.token}


async def _token(
    sandbox: Any,
    computer_id: str,
    user_id: str,
    base_url: str,
    *,
    renewing: bool = False,
) -> tuple[datetime | None, dict[str, Any] | None]:
    """The token the sandbox is to hold, with the config to publish when it
    is a new one: the stored token while the sandbox took it and it has time
    left, else a fresh mint. A turn's path keeps one that only runs low,
    which ``renew`` replaces off it. Called under the folders lock."""
    expires_at, held_by = await db.current_token(computer_id)
    low = runs_low if renewing else lapsed
    if not low(expires_at) and held_by == sandbox_id(sandbox):
        return expires_at, None
    minted = await tokens.mint_token(computer_id, user_id)
    return minted.expires_at, _config(base_url, minted)


async def _publishing(
    sandbox: Any,
    computer_id: str,
    user_id: str,
    base_url: str,
    run: Callable[[dict[str, Any] | None], Awaitable[MountOutcome]],
    *,
    renewing: bool = False,
) -> tuple[MountOutcome, datetime | None]:
    """Run one daemon command carrying the token the sandbox is to hold.
    A renewal runs none when the sandbox took a token with time left.

    Called under the folders lock, which makes each mint and its publish one
    turn per computer: two at once could each push the other's token out of
    both slots.
    """
    expires_at, config = await _token(
        sandbox, computer_id, user_id, base_url, renewing=renewing
    )
    if renewing and config is None:
        return MountOutcome(ok=True), expires_at
    outcome = await run(config)
    if outcome.error == MountError.NO_CONFIG and config is None:
        # A sandbox rebuilt under a live token has no copy of it.
        minted = await tokens.mint_token(computer_id, user_id)
        config, expires_at = _config(base_url, minted), minted.expires_at
        outcome = await run(config)
    if config is not None and outcome.error not in _NOT_INSTALLED:
        await db.mark_held(computer_id, sandbox_id(sandbox), expires_at)
    return outcome, expires_at


def _not_up(computer_id: str, outcome: Any) -> None:
    logger.warning(
        "livefs mount not up for computer %s: %s %s %s",
        computer_id,
        outcome.error,
        outcome.reason or "",
        getattr(outcome, "failed", None) or "",
    )


async def _start(
    sandbox: Any,
    computer_id: str,
    user_id: str,
    base_url: str,
    *,
    wait_s: float | None = TURN_LOCK_WAIT_S,
    renewing: bool = False,
) -> tuple[MountOutcome, datetime | None] | None:
    """Install the token and start the daemon, or None when a settle kept
    the folders past ``wait_s`` (None waits the lock's own default)."""
    wait = {} if wait_s is None else {"wait_s": wait_s}
    async with workspace_folders_lock(computer_id, **wait) as folders:
        if folders is None:
            return None
        return await _publishing(
            sandbox,
            computer_id,
            user_id,
            base_url,
            lambda config: livefs_mount.start(sandbox, config=config, base_url=base_url),
            renewing=renewing,
        )


async def serve(
    sandbox: Any,
    *,
    computer_id: str,
    user_id: str | None,
    provider: str,
) -> MountState:
    """Make the mount serve with a token that is not gone: install one when
    the sandbox holds none it took with time left, and start the daemon if
    it is not serving current code. The server address goes with every
    call, so a changed one reaches a daemon whose token has time left. A
    sync still replacing the daemon's code answers ``stale_code``.
    """
    computer_id = str(computer_id)
    if not _usable(sandbox, user_id):
        return _off(computer_id, "no sandbox or owner")
    base_url = effective_relay_base_url(provider)
    try:
        started = await _start(sandbox, computer_id, user_id, base_url)
    except Exception as exc:
        logger.warning(
            "livefs start failed for computer %s: %s", computer_id, exc, exc_info=True
        )
        return MountState(False, reason=str(exc))
    if started is None:
        return MountState(False, reason="folders are moving", error=MountError.BUSY)
    outcome, expires_at = started
    if not outcome.ok:
        _not_up(computer_id, outcome)
    elif outcome.started:
        logger.info("livefs serving on computer %s", computer_id)
    return MountState(
        outcome.ok,
        expires_at,
        outcome.reason or outcome.error,
        error=outcome.error,
        started=outcome.started,
    )


def _linked(
    links: dict[str | None, list[tuple[str, str]]], failed: dict[str, str] | None
) -> LinkState:
    """Which folders' links went in, given the targets that failed. One of
    the computer's own failing counts against every folder, since a folder
    that owns the root reads its files through those."""
    missing = {posixpath.normpath(target) for target in failed or ()}
    lost = frozenset(
        folder
        for folder, group in links.items()
        if any(posixpath.normpath(target) in missing for _, target in group)
    )
    if None in lost:
        lost = frozenset(links)
    return LinkState(frozenset(links) - lost, lost)


async def link(sandbox: Any, *, computer_id: str, user_id: str | None) -> LinkState:
    """Link exactly the computer's live folders into the mount. Asked only
    once the mount serves: a link sets aside what sat at its path, which on
    a sandbox the mount never served would hide the files there for nothing.
    """
    from src.server.app import setup

    computer_id = str(computer_id)
    if not _usable(sandbox, user_id):
        return LinkState(reason="no sandbox or owner")
    try:
        # The links go into every folder on the computer, so no settle may
        # move one between reading where it is and linking into it: a stale
        # read recreates the folder that moved, or points a sibling that took
        # its name at the other workspace's files.
        async with workspace_folders_lock(
            computer_id, wait_s=TURN_LOCK_WAIT_S
        ) as folders:
            if folders is None:
                return LinkState(error=MountError.BUSY, reason="folders are moving")
            identity = tokens.LivefsIdentity(computer_id, user_id, sandbox.layout.root)
            links = await LivefsTree(identity, setup.store).links()
            outcome = await livefs_mount.link(
                sandbox, [link for group in links.values() for link in group]
            )
    except Exception as exc:
        logger.warning(
            "livefs links failed for computer %s: %s", computer_id, exc, exc_info=True
        )
        return LinkState(reason=str(exc))
    if outcome.set_aside:
        logger.warning(
            "livefs moved files that sat where its links go on computer %s: %s",
            computer_id,
            outcome.set_aside,
        )
    if not outcome.ok:
        _not_up(computer_id, outcome)
    if outcome.error not in (None, MountError.LINK_FAILED):
        return LinkState(error=outcome.error, reason=outcome.reason)
    went = _linked(links, outcome.failed)
    return replace(went, error=outcome.error, reason=outcome.reason)


async def renew(
    sandbox: Any,
    *,
    computer_id: str,
    user_id: str | None,
    provider: str,
) -> MountState | None:
    """Give a serving mount a fresh token before its own runs out, off every
    turn's path. Answers the mount with the token the sandbox then holds, not
    mounted when the daemon took the token but cannot serve with it, or None
    when this try renewed nothing (the lock stayed held, or it failed), for
    the caller to try again later.

    A token another worker minted and published is adopted without a lock
    or an exec, and a renewal that waited out another's lock adopts its
    token under it. Only the token changes, and the daemon rereads it on its
    own, so the links stay as they are.
    """
    computer_id = str(computer_id)
    if not _usable(sandbox, user_id):
        return None
    try:
        expires_at, held_by = await db.current_token(computer_id)
        if not runs_low(expires_at) and held_by == sandbox_id(sandbox):
            return MountState(True, expires_at)
        base_url = effective_relay_base_url(provider)
        started = await _start(
            sandbox, computer_id, user_id, base_url, wait_s=None, renewing=True
        )
    except tokens.NotServing as exc:
        logger.info("livefs token not renewed: %s", exc)
        return None
    except Exception as exc:
        logger.warning("livefs token not renewed for computer %s: %s", computer_id, exc)
        return None
    if started is None:
        return None
    outcome, expires_at = started
    if not outcome.ok:
        _not_up(computer_id, outcome)
    if outcome.error in _NOT_INSTALLED:
        return None
    return MountState(
        outcome.ok, expires_at, outcome.reason or outcome.error, error=outcome.error
    )


class Handle:
    """The serving mount as the file tools see it (``MountHandle``), on the
    worker running the turn. Execution context only: ``serve``, ``link`` and
    ``renew`` go through the manager to the functions here, whose locks,
    token table and daemon state are the truth, and each lands in
    ``tracker``."""

    def __init__(
        self,
        computer_id: str,
        tracker: MountTracker,
        sandbox: Any,
        *,
        serve: Callable[[], Awaitable[MountState]],
        link: Callable[[], Awaitable[LinkState]],
        renew: Callable[[], Awaitable[MountState | None]],
    ) -> None:
        self._computer_id = computer_id
        self._tracker = tracker
        self._sandbox = sandbox
        self._serve = serve
        self._link = link
        self._renew = renew

    async def ready(self, workspace_id: str | None = None) -> bool:
        tracker, sandbox = self._tracker, self._sandbox
        await tracker.keep_token(self._serve, self._renew)
        if not tracker.serves(sandbox, workspace_id):
            # Links still going in, or a folder whose links a backoff held off
            # (or a refresh just brought the mount back for), go in now
            # rather than on the next turn.
            tracker.link_soon(sandbox, workspace_id, self._link)
            if tracker.linking():
                began = time.monotonic()
                await tracker.linked()
                logger.info(
                    "livefs command waited %.0f ms for the links on computer %s",
                    (time.monotonic() - began) * 1000,
                    self._computer_id,
                )
                # A token that failed renewals can lapse during the wait.
                await tracker.keep_token(self._serve, self._renew)
        return tracker.serves(sandbox, workspace_id)

    async def prepare(
        self, call_id: str | None = None, context: CallContext | None = None
    ) -> None:
        if call_id and context is not None:
            await outcomes.open_call(self._computer_id, call_id, context)

    async def report(
        self, call_id: str, output: str, context: CallContext | None = None
    ) -> str:
        thread_id = context.thread_id if context is not None else None
        text = outcomes.describe(
            await outcomes.collect(self._computer_id, call_id, thread_id)
        )
        # A killed daemon leaves the links answering ENOTCONN until a start
        # replaces it, and a warm turn asks none, so the command that meets
        # it does. The links point through the mount's own link, which the
        # start swaps, so they stay.
        if _met_dead_mount(output):
            state = await self._tracker.restart(self._serve)
            if state.started:
                note = "The file mount had stopped and is serving again; rerun the command."
                text = f"{text}\n\n{note}" if text else note
        return text

    async def save_transcript(
        self, target: TranscriptTarget, messages: Sequence[AnyMessage]
    ) -> bool:
        return await transcripts.save_live(target, messages)


async def revoke(computer_id: Any) -> None:
    """End every token the computer holds; a stopped or deleted one serves nothing."""
    try:
        await tokens.revoke(str(computer_id))
    except Exception:
        logger.warning("livefs token not revoked for computer %s", computer_id, exc_info=True)
