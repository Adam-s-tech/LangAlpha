"""The symlinks that put the mount where the agent's paths are."""

from __future__ import annotations

import contextlib
import os


def swap_link(value: str, path: str, owner: os.stat_result | None = None) -> None:
    """Make ``path`` a symlink to ``value`` in one step."""
    tmp = f"{path}.livefs.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(value, tmp)
    try:
        if owner is not None:
            os.lchown(tmp, owner.st_uid, owner.st_gid)
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def _makedirs(path: str, owner: os.stat_result) -> None:
    """Create what is missing of ``path``, owned like the root, since the
    sandbox's own user writes beside the link."""
    missing = []
    while not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for directory in reversed(missing):
        os.mkdir(directory)
        os.chown(directory, owner.st_uid, owner.st_gid)


def _set_aside(target: str) -> str:
    """Keep what was already where a link goes, under a name beside it."""
    aside, n = f"{target}.local", 0
    while os.path.lexists(aside):
        n += 1
        aside = f"{target}.local.{n}"
    os.rename(target, aside)
    return aside


def link(
    value: str, target: str, owner: os.stat_result, *, replace: bool = False
) -> str | None:
    """Point ``target`` at ``value``; returns where anything already there
    went. With ``replace``, a file there is an older copy of what the link
    serves and is removed instead."""
    aside = None
    if os.path.islink(target):
        if os.readlink(target) == value:
            return None
    elif replace and os.path.isfile(target):
        os.unlink(target)
    elif os.path.isdir(target):
        try:
            os.rmdir(target)
        except OSError:
            aside = _set_aside(target)
    elif os.path.lexists(target):
        aside = _set_aside(target)
    else:
        _makedirs(os.path.dirname(target), owner)
    swap_link(value, target, owner)
    return aside


def unlink(target: str, mount: str) -> None:
    """Remove a link into ``mount``, and nothing that has since replaced it."""
    with contextlib.suppress(OSError):
        if os.readlink(target).startswith(mount + "/"):
            os.unlink(target)
