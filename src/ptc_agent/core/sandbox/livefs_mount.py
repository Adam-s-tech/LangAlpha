"""Run the file mount's own commands in a sandbox.

The server decides what to mount and mints the token; this only carries them
into the sandbox. The daemon's commands are idempotent, so every caller asks
for the state it wants and the daemon does whatever part of it is missing.
"""

from __future__ import annotations

import json
import secrets
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox.livefs_runtime import protocol
from ptc_agent.core.sandbox.retry import RetryPolicy

if TYPE_CHECKING:
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

# Covers a cold start (the daemon waits up to 10 s for its mount) plus links.
_EXEC_TIMEOUT_S = 30


class MountHandle(Protocol):
    """What a tool asks of a serving mount around one command. The host
    implements it, since the token and the save outcomes are its own."""

    async def prepare(self) -> None:
        """Keep the token good for the command about to run."""

    async def report(self, call_id: str, output: str) -> str:
        """Saves through the mount that failed during ``call_id`` (and any
        that failed after an earlier command returned), as text for the tool
        result; empty when none did. An ``output`` showing the mount dead
        restarts it."""

    async def save_transcript(self, target: Any, messages: list[Any]) -> bool:
        """Store one agent's transcript (a ``TranscriptTarget``) from the
        messages in hand, where the mount serves it; whether it landed."""


def new_call_id() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True)
class MountOutcome:
    ok: bool
    #: no_config, bad_config, unsupported, installing, unreachable,
    #: start_failed, link_failed, or unanswered when the command printed
    #: nothing the host could read.
    error: str | None = None
    reason: str | None = None
    started: bool = False
    failed: dict[str, str] | None = None
    #: Link paths that already held files, and where those were moved.
    set_aside: dict[str, str] | None = None


def _command(layout: SandboxLayout, action: str, *args: str) -> str:
    src = shlex.quote(layout.internal_src)
    argv = " ".join(shlex.quote(a) for a in (action, "--root", layout.root, *args))
    return f"PYTHONPATH={src} python3 -m {protocol.PACKAGE_NAME} {argv}"


def _parse(result: Any) -> dict[str, Any] | None:
    lines = (getattr(result, "stdout", "") or "").strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


async def up(
    sandbox: PTCSandbox,
    *,
    config: dict[str, Any] | None,
    links: Sequence[tuple[str, str]],
    replace: Sequence[str] = (),
) -> MountOutcome:
    """Make the mount serve and link exactly ``links`` (mount path, target).
    A file at a ``replace`` target is removed rather than set aside.

    ``config`` is a new token to install; None keeps the one the sandbox
    has. It travels under a random name, and the daemon takes it into a
    directory only root can read and deletes the staged copy.
    """
    assert sandbox.runtime is not None
    args: list[str] = []
    staged = None
    if config is not None:
        staged = f"{sandbox.layout.internal}/.livefs.{secrets.token_hex(16)}.stage"
        await sandbox._runtime_call(
            sandbox.runtime.upload_file,
            json.dumps(config).encode(),
            staged,
            retry_policy=RetryPolicy.SAFE,
        )
        args += ["--stage", staged]
    for source, target in links:
        args += ["--link", f"{source}:{target}"]
    for target in replace:
        args += ["--replace", target]
    shell = _command(sandbox.layout, "up", *args)
    if staged:
        # A command that dies before the move must not leave the token behind.
        shell += f"; rc=$?; rm -f {shlex.quote(staged)}; exit $rc"
    result = await sandbox._runtime_call(
        sandbox.runtime.exec_as_root,
        shell,
        _EXEC_TIMEOUT_S,
        retry_policy=RetryPolicy.SAFE,
    )
    answer = _parse(result)
    if answer is None:
        return MountOutcome(
            ok=False,
            error="unanswered",
            reason=(getattr(result, "stderr", "") or "")[-300:],
        )
    return MountOutcome(
        ok=bool(answer.get("ok")),
        error=answer.get("error") or ("link_failed" if answer.get("failed") else None),
        reason=answer.get("reason"),
        started=bool(answer.get("started")),
        failed=answer.get("failed"),
        set_aside=answer.get("set_aside"),
    )

