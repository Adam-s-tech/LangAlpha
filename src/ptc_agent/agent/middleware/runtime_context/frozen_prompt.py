"""The static system prompt as the thread's epoch froze it.

The static prompt is built with each turn's agent, from what holds at the
build, and it is the cached prefix. A value it states that can flip between
two turns (whether the file mount serves) is frozen into the epoch instead,
and the prompt is sent rendered with the frozen value whenever the build saw
another, so the prefix changes only when the epoch is rebuilt.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from ptc_agent.agent.middleware.runtime_context.epoch import BaselineEpoch
from ptc_agent.agent.middleware.runtime_context.state import STATE_BASELINE, state_get

logger = logging.getLogger(__name__)


class FrozenPromptMiddleware(AgentMiddleware):
    """Send the static prompt with the epoch's ``files_mounted``.

    Outermost on the system message: it replaces the prompt the agent was
    built with, and every middleware inside it appends to the replacement.

    Args:
        files_mounted: The value the built prompt states.
        render: The static prompt stating the value given, built the way the
            agent's own was.
    """

    def __init__(self, *, files_mounted: bool, render: Callable[[bool], str]) -> None:
        super().__init__()
        self._built = files_mounted
        self._render = render
        self._rendered: dict[bool, str] = {}

    def _frozen_prompt(self, request: ModelRequest) -> str | None:
        epoch = BaselineEpoch.from_state(
            state_get(getattr(request, "state", None), STATE_BASELINE)
        )
        frozen = epoch.files_mounted
        if frozen is None or frozen == self._built:
            return None
        if frozen not in self._rendered:
            self._rendered[frozen] = self._render(frozen)
        return self._rendered[frozen]

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        # Sync fallback: the async agent won't call this but the protocol requires it.
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        try:
            prompt = self._frozen_prompt(request)
        except Exception:  # noqa: BLE001 - the built prompt is still a whole prompt
            logger.warning("[FrozenPrompt] render failed; sending the built prompt", exc_info=True)
            return await handler(request)
        if prompt is None:
            return await handler(request)
        return await handler(request.override(system_message=SystemMessage(content=prompt)))
