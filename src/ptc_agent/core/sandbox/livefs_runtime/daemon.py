"""Mount the user's server-held files (memory, profile, workflows, transcripts)
in the sandbox.

Runs as root, from the copy ``boot`` checked; every read and save goes to the
server's livefs endpoint with the computer's mount token, and nothing is kept
on disk.

    python3 -m livefs start --root R --base-url U [--stage S]
    python3 -m livefs link --root R [--link SRC:DST ...] [--replace DST ...]
    python3 -m livefs down --root R
    python3 -m livefs status --root R
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
from collections.abc import Callable

from .lifecycle import (
    Paths,
    code_version,
    has_libfuse,
    healthy,
    install_config,
    install_libfuse,
    locked,
    read_state,
    rebase,
    retire,
    serve,
    start,
    sweep,
    unsupported,
    write_state,
)
from .links import link, unlink
from .protocol import MountError
from .remote import Remote

PROBE_TIMEOUT_S = 5


def _probe(paths: Paths) -> str | None:
    """Why the server cannot be reached with the installed config, if it
    cannot. A mount that cannot reach it would hold every file operation for
    the request timeout, where no mount refuses at once."""
    try:
        status, _, _ = Remote(paths.config, PROBE_TIMEOUT_S).request(
            "GET", "list", {"path": ""}, attempts=1
        )
    except OSError:
        with open(paths.config) as f:
            return f"no answer from {json.load(f).get('base_url')}"
    return None if status == 200 else f"the server answered {status}"


def _probing(paths: Paths) -> Callable[[], str | None]:
    """Start ``_probe`` beside the rest of ``start``; the call returned waits
    for its answer. Its request is mostly waiting on the network, which a
    new daemon's start fills."""
    answer: list[str | None] = []

    def run() -> None:
        try:
            answer.append(_probe(paths))
        except Exception as exc:  # the config went unreadable mid-probe
            answer.append(f"the probe failed: {exc}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def result() -> str | None:
        thread.join()
        return answer[0]

    return result


def _ready(args, paths: Paths) -> tuple[dict | None, Callable[[], str | None] | None]:
    """Everything before the lock: the answer when the mount cannot serve
    yet, else the probe a start waits on (None when there is nothing to
    probe). A staged token goes in first, so it reaches the sandbox whatever
    else holds the mount back."""
    reason = unsupported()
    if reason:
        return {"ok": False, "error": MountError.UNSUPPORTED, "reason": reason}, None
    os.makedirs(paths.private, mode=0o700, exist_ok=True)
    if args.stage:
        problem = install_config(args.stage, paths)
        if problem:
            return {"ok": False, "error": MountError.BAD_CONFIG, "reason": problem}, None
    if not os.path.exists(paths.config):
        return {"ok": False, "error": MountError.NO_CONFIG}, None
    if not has_libfuse():
        install_libfuse(paths)
        return {
            "ok": False,
            "error": MountError.INSTALLING,
            "reason": "installing libfuse",
        }, None
    rebased = rebase(args.base_url, paths)
    changed = args.stage or rebased or not healthy(paths.mount)
    probe = _probing(paths) if changed else None
    return None, probe


def _serving(
    args, paths: Paths, state: dict, probe: Callable[[], str | None] | None
) -> tuple[bool, dict | None]:
    """Under the lock: start the daemon if it is not serving current code,
    and record what the probe answered. Whether one was started, and why the
    mount does not serve if it does not."""
    if probe is None and state.get("unreachable"):
        # A probe that failed is asked again until one answers, whichever
        # host asks: a daemon up since then serves nothing it can save. Read
        # under the lock, so a start that waited on the one that failed asks.
        probe = _probing(paths)
    try:
        started, problem = start(args, state, paths, gate=probe)
    except OSError as exc:
        started, problem = True, str(exc)
    unreachable = probe() if probe else None
    if unreachable:
        state["unreachable"] = unreachable
    elif probe:
        state.pop("unreachable", None)
    if unreachable:
        return started, {"ok": False, "error": MountError.UNREACHABLE, "reason": unreachable}
    if problem:
        return started, {"ok": False, "error": MountError.START_FAILED, "reason": problem}
    return started, None


def link_folders(args, paths: Paths) -> dict:
    """Link exactly ``--link``.

    Links an earlier call made that this one does not ask for are removed, so
    the caller's list (every live folder on this computer) is the whole truth
    and a folder that left needs no separate teardown. Laid whether or not the
    daemon serves: each points through the mount link, which a start swaps
    over, so they stay right across a restart.
    """
    os.makedirs(paths.private, mode=0o700, exist_ok=True)
    with locked(paths):
        state = read_state(paths)
        wanted = {}
        for spec in args.link or ():
            source, _, target = spec.partition(":")
            wanted[os.path.abspath(target)] = source
        for target in state.get("links", ()):
            if target not in wanted:
                unlink(target, paths.mount)
        owner = os.stat(args.root)
        replace = {os.path.abspath(t) for t in args.replace or ()}
        failed, moved = {}, {}
        for target, source in wanted.items():
            try:
                aside = link(
                    os.path.join(paths.mount, source),
                    target,
                    owner,
                    replace=target in replace,
                )
            except OSError as exc:
                failed[target] = str(exc)
            else:
                if aside:
                    moved[target] = aside
        state["links"] = sorted(wanted)
        # What the daemon knows of the layout before it asks (``LiveFS``).
        state["sources"] = sorted(set(wanted.values()))
        write_state(paths, state)
    return {
        "ok": not failed,
        "error": MountError.LINK_FAILED if failed else None,
        "failed": failed or None,
        "set_aside": moved or None,
    }


def start_mount(args, paths: Paths) -> dict:
    """Stage the token and make sure the mount serves current code. It needs
    no folder list, so the host runs it beside the asset sync. Beside a sync
    still replacing the code, ``boot`` answers ``stale_code`` and the host
    asks again once the sync has landed."""
    refused, probe = _ready(args, paths)
    if refused:
        return refused
    with locked(paths):
        state = read_state(paths)
        started, refused = _serving(args, paths, state, probe)
        write_state(paths, state)
    return refused or {"ok": True, "error": None, "started": started or None}


def down(args, paths: Paths) -> dict:
    """Remove every link and stop serving."""
    os.makedirs(paths.private, mode=0o700, exist_ok=True)
    with locked(paths):
        state = read_state(paths)
        for target in state.get("links", ()):
            unlink(target, paths.mount)
        with contextlib.suppress(OSError):
            os.unlink(paths.mount)
        retire(state, grace=0)
        sweep("", paths)
        write_state(paths, {})
    return {"ok": True, "unlinked": state.get("links", [])}


def status(args, paths: Paths) -> dict:
    state = read_state(paths)
    return {
        "ok": healthy(paths.mount),
        "mount": state.get("mount"),
        "links": state.get("links", []),
        "code": state.get("code"),
        "current": code_version(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="livefs")
    parser.add_argument("command", choices=("serve", "start", "link", "down", "status"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--mount")
    parser.add_argument("--stage")
    parser.add_argument("--base-url")
    parser.add_argument("--link", action="append")
    parser.add_argument("--replace", action="append")
    parser.add_argument("--gated", action="store_true")
    args = parser.parse_args()
    paths = Paths()
    if args.command == "serve":
        serve(args, paths)
        return 0
    commands = {"start": start_mount, "link": link_folders, "down": down, "status": status}
    result = commands[args.command](args, paths)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1
