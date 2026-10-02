"""The symlinks that put the mount where the agent's paths are."""

from __future__ import annotations

import contextlib
import os


def swap_link(value: str, path: str) -> None:
    """Make ``path`` a symlink to ``value`` in one step."""
    tmp = f"{path}.livefs.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(value, tmp)
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def _set_aside(target: str) -> str:
    """Keep what was already where a link goes, under a name beside it."""
    aside, n = f"{target}.local", 0
    while os.path.lexists(aside):
        n += 1
        aside = f"{target}.local.{n}"
    os.rename(target, aside)
    return aside


def link(value: str, target: str) -> str | None:
    """Point ``target`` at ``value``; returns where anything already there
    went. Made with the caller's rights, as whoever owns the folders."""
    aside = None
    if os.path.islink(target):
        if os.readlink(target) == value:
            return None
    elif os.path.isdir(target):
        try:
            os.rmdir(target)
        except OSError:
            aside = _set_aside(target)
    elif os.path.lexists(target):
        aside = _set_aside(target)
    else:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    swap_link(value, target)
    return aside


def unlink(target: str, mount: str) -> None:
    """Remove a link into ``mount``, and nothing that has since replaced it."""
    with contextlib.suppress(OSError):
        if os.readlink(target).startswith(mount + "/"):
            os.unlink(target)
