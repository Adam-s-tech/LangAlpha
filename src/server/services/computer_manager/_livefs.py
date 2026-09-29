"""Seam: each machine's file mount, brought up when due and revoked with the machine.

One file of the ComputerManager split; see the package __init__. What this
worker saw of the mount is ``MachineState.livefs``, a ``MountTracker``."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ptc_agent.core.sandbox.livefs_runtime.protocol import MountError

if TYPE_CHECKING:
    from src.server.services.livefs.mount import Handle, LinkState, MountState

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class LivefsMixin:
    def _livefs_provider(self, sandbox: Any) -> str:
        provider = getattr(getattr(sandbox, "config", None), "sandbox", None)
        return getattr(provider, "provider", None) or self.config.sandbox.provider

    async def _serve_livefs(
        self, computer_id: Any, user_id: str | None, sandbox: Any
    ) -> MountState:
        """Make the machine's file mount serve, record it, and hand the file
        tools the mount (``sandbox.livefs``) while it does.

        Never raises: without the mount the file tools still reach these files
        through the store, so a mount failure costs code access, not a turn.
        """
        from src.server.services.livefs import mount as livefs

        state = await livefs.serve(
            sandbox,
            computer_id=str(computer_id),
            user_id=user_id,
            provider=self._livefs_provider(sandbox),
        )
        self._machine(str(computer_id)).livefs.saw(sandbox, state)
        # A settle holding the folders asked nothing, so whatever the sandbox
        # was handed stands.
        if sandbox is not None and state.error != MountError.BUSY:
            sandbox.livefs = (
                self._livefs_handle(computer_id, user_id, sandbox)
                if state.mounted
                else None
            )
        if state.mounted:
            # A turn's path keeps a token that only runs low; this replaces it.
            self._renew_livefs_soon(computer_id, user_id, sandbox)
        return state

    def _livefs_link(
        self, computer_id: Any, user_id: str | None, sandbox: Any
    ) -> Callable[[], Awaitable[LinkState]]:
        from src.server.services.livefs import mount as livefs

        return lambda: livefs.link(
            sandbox, computer_id=str(computer_id), user_id=user_id
        )

    def _link_livefs_soon(
        self,
        computer_id: Any,
        user_id: str | None,
        sandbox: Any,
        workspace_id: str | None = None,
    ) -> None:
        """Link the machine's folders in off the turn's path, when the mount
        serves and ``workspace_id``'s folder (None: any) is not seen linked.
        A command in that workspace waits for it (``Handle.ready``)."""
        self._machine(str(computer_id)).livefs.link_soon(
            sandbox, workspace_id, self._livefs_link(computer_id, user_id, sandbox)
        )

    def _livefs_handle(self, computer_id: Any, user_id: str | None, sandbox: Any) -> Handle:
        from src.server.services.livefs import mount as livefs

        return livefs.Handle(
            str(computer_id),
            self._machine(str(computer_id)).livefs,
            sandbox,
            serve=lambda: self._serve_livefs(computer_id, user_id, sandbox),
            link=self._livefs_link(computer_id, user_id, sandbox),
            renew=lambda: self._renew_livefs(computer_id, user_id, sandbox),
        )

    def _livefs_owed(self, computer_id: Any, workspace_id: str, sandbox: Any) -> bool:
        """Whether a turn in ``workspace_id`` has anything to ask of the mount."""
        tracker = self._machine(str(computer_id)).livefs
        return (
            tracker.serve_due(sandbox)
            or tracker.link_owed(sandbox, workspace_id)
            or tracker.renewal_owed(sandbox)
        )

    async def _keep_livefs(
        self, computer_id: Any, user_id: str | None, sandbox: Any, workspace_id: str
    ) -> None:
        """A warm turn's share of the mount. A mount not serving is waited
        for, since the prompt the turn builds says whether the files are
        mounted. A token running low or gone and a folder not linked yet are
        seen to off its path, and a command waits for what it needs of them
        (``Handle.ready``)."""
        if sandbox is None or not user_id:
            return
        tracker = self._machine(str(computer_id)).livefs
        if tracker.serve_due(sandbox) and not tracker.serving(sandbox):
            await self._serve_livefs(computer_id, user_id, sandbox)
        self._renew_livefs_soon(computer_id, user_id, sandbox)
        self._link_livefs_soon(computer_id, user_id, sandbox, workspace_id)

    async def _livefs_beside(
        self,
        computer_id: Any,
        user_id: str | None,
        sandbox: Any,
        sync: Awaitable[_T],
        *,
        workspace_id: str | None = None,
    ) -> _T:
        """Run an asset sync for code about to run on ``sandbox``, and make
        the file mount serve beside it when due. The daemon's start makes no
        links, so it runs beside the sync; the links wait for the layout the
        sync may move, since a migration moving files under a link would move
        them through the mount, and then go in off the turn's path. A start
        the sync may have stood in the way of (its code still being replaced,
        a settle holding the folders, no clear answer) is asked once more
        after it: the turn's prompt says whether the files are mounted, and
        keeps saying it for the rest of the conversation.
        """
        if sandbox is None or not user_id:
            return await sync
        if not self._machine(str(computer_id)).livefs.serve_due(sandbox):
            result = await sync
            self._renew_livefs_soon(computer_id, user_id, sandbox)
            self._link_livefs_soon(computer_id, user_id, sandbox, workspace_id)
            return result
        times: dict[str, float] = {}

        async def timed(name: str, step: Awaitable[MountState]) -> MountState:
            began = time.monotonic()
            try:
                return await step
            finally:
                times[name] = (time.monotonic() - began) * 1000

        serving = asyncio.ensure_future(
            timed("livefs_start", self._serve_livefs(computer_id, user_id, sandbox))
        )
        try:
            result = await sync
        except BaseException:
            serving.cancel()
            raise
        state = await serving
        if not state.settled:
            await timed(
                "livefs_retry", self._serve_livefs(computer_id, user_id, sandbox)
            )
        self._link_livefs_soon(computer_id, user_id, sandbox, workspace_id)
        logger.info(
            "[SYNC_DETAIL] computer_id=%s workspace_id=%s %s",
            computer_id,
            workspace_id,
            " ".join(f"{k}={v:.0f}ms" for k, v in times.items()),
        )
        return result

    async def _renew_livefs(
        self, computer_id: Any, user_id: str | None, sandbox: Any
    ) -> MountState | None:
        from src.server.services.livefs import mount as livefs

        return await livefs.renew(
            sandbox,
            computer_id=str(computer_id),
            user_id=user_id,
            provider=self._livefs_provider(sandbox),
        )

    def _renew_livefs_soon(
        self, computer_id: Any, user_id: str | None, sandbox: Any
    ) -> None:
        """Renew the mount's token in the background once it runs low; the
        turn goes on with the current one."""
        if not user_id:
            return
        self._machine(str(computer_id)).livefs.renew_soon(
            lambda: self._renew_livefs(computer_id, user_id, sandbox), sandbox
        )

    async def _revoke_livefs(self, computer_id: Any) -> None:
        """A machine leaving service stops serving its files at once, rather
        than when the token it holds would have run out."""
        from src.server.services.livefs import mount as livefs

        await livefs.revoke(computer_id)
        machine = self._machine_if_known(str(computer_id))
        if machine is not None:
            machine.livefs.forget()
