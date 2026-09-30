"""Starting, replacing and stopping the daemon that serves the mount, and the
root-only files it keeps."""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from .links import swap_link
from .protocol import GENERATIONS, MOUNT

START_TIMEOUT_S = 10
RETIRE_AFTER_S = 30

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _package_files() -> list[tuple[str, bytes]]:
    files = []
    for name in sorted(os.listdir(_PACKAGE_DIR)):
        if name.endswith(".py"):
            with open(os.path.join(_PACKAGE_DIR, name), "rb") as f:
                files.append((name, f.read()))
    return files


def _version(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, data in files:
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()[:16]


def code_version() -> str:
    """What a running daemon is compared against, so a shipped change restarts it."""
    return _version(_package_files())


def code_manifest() -> dict[str, str]:
    """Each file's digest, which ``boot`` checks the shipped copy against."""
    return {name: hashlib.sha256(data).hexdigest() for name, data in _package_files()}


@dataclass(frozen=True)
class Paths:
    #: The link every served path goes through.
    mount: str = MOUNT
    #: Each start mounts a fresh directory here and repoints ``mount`` at it,
    #: so a restart never touches the links and nothing ever has to be
    #: unmounted. Some runtimes refuse to unmount a FUSE mount from inside
    #: the sandbox (Sysbox answers ENOENT); there a replaced daemon's mount
    #: stays, unreachable, until the sandbox restarts.
    generations: str = GENERATIONS
    #: The token, the state naming what root may unmount, kill and unlink,
    #: the lock and the log: root's alone. In a directory the sandbox's own
    #: user can write, a planted symlink or a forged state file would turn
    #: the daemon's root operations into theirs.
    private: str = "/var/lib/livefs"

    @property
    def config(self) -> str:
        return os.path.join(self.private, "config.json")

    @property
    def state(self) -> str:
        return os.path.join(self.private, "state.json")

    @property
    def log(self) -> str:
        return os.path.join(self.private, "log")

    @property
    def code(self) -> str:
        return os.path.join(self.private, "code")


# --- the mount table ----------------------------------------------------


def _unescape(field: str) -> str:
    """/proc/self/mounts writes space, tab, newline and backslash as octal."""
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def _mounts() -> list[str]:
    with open("/proc/self/mounts") as f:
        return [
            _unescape(fields[1]) for fields in (line.split() for line in f) if len(fields) >= 2
        ]


def mounted(path: str) -> bool:
    return os.path.realpath(path) in _mounts()


def healthy(path: str) -> bool:
    """Mounted and answering: a dead daemon leaves a mount that errors ENOTCONN."""
    if not mounted(path):
        return False
    try:
        os.stat(path)
    except OSError:
        return False
    return True


# --- libfuse ------------------------------------------------------------

# Version 3 only, found by this name and installed as the fuse3 package: the
# save rules rely on its open carrying O_TRUNC.
LIBFUSE = "fuse3"
#: Tried first: ``find_library`` runs ``ldconfig -p`` to answer.
_SONAMES = ("libfuse3.so.3", "libfuse3.so")

_INSTALL_RETRY_S = 600
_APT_LOCK_WAIT = "-o DPkg::Lock::Timeout=120"


def libfuse() -> str | None:
    """What loads libfuse 3 here, if anything does."""
    for name in _SONAMES:
        try:
            ctypes.CDLL(name)
        except OSError:
            continue
        return name
    from ctypes.util import find_library

    return find_library(LIBFUSE)


def has_libfuse() -> bool:
    return libfuse() is not None


def use_libfuse3() -> None:
    """Point mfusepy, which loads libfuse on import, at libfuse 3. It
    prefers 2 when both are installed, and 2 truncates in a separate call
    after open, which would make a redirect's empty save look written (see
    ``LiveFS.flush``)."""
    found = libfuse()
    if found in _SONAMES:
        os.environ["FUSE_LIBRARY_PATH"] = found
    else:
        os.environ["FUSE_LIBRARY_NAME"] = LIBFUSE


def unsupported() -> str | None:
    """Why this sandbox cannot mount, answered before a start that would
    only time out."""
    if os.geteuid() != 0:
        return "not running as root"
    if not os.path.exists("/dev/fuse"):
        return "no /dev/fuse"
    return None


def install_libfuse(paths: Paths) -> None:
    """Install libfuse in the background on a sandbox built before its image
    carried it. At most once per retry window, so a sandbox without package
    access is not asked again every turn."""
    marker = os.path.join(paths.private, "install")
    try:
        if time.time() - os.stat(marker).st_mtime < _INSTALL_RETRY_S:
            return
    except OSError:
        pass
    with open(marker, "w") as f:
        f.write(str(os.getpid()))
    log = open(paths.log, "ab")
    # The package lists stay: the sandbox is the user's machine, and its lists
    # may be ones they refreshed. The lock wait lets a package install of
    # theirs finish rather than fail this one for the whole retry window.
    subprocess.Popen(
        [
            "sh",
            "-c",
            f"apt-get {_APT_LOCK_WAIT} update -qq && DEBIAN_FRONTEND=noninteractive"
            f" apt-get {_APT_LOCK_WAIT} install -y -qq --no-install-recommends fuse3",
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        cwd="/",
        start_new_session=True,
        close_fds=True,
    )


# --- root's own files ---------------------------------------------------


@contextlib.contextmanager
def locked(paths: Paths):
    fd = os.open(os.path.join(paths.private, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_state(paths: Paths) -> dict:
    try:
        with open(paths.state) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_private(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_state(paths: Paths, state: dict) -> None:
    _write_private(paths.state, json.dumps(state).encode())


def install_config(stage: str, paths: Paths) -> str | None:
    """Take the token the host staged into the sandbox, leaving no copy
    behind. Read without following a link, so a path planted in its place
    names no other file."""
    try:
        fd = os.open(stage, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return f"staged config unreadable: {exc.strerror}"
    try:
        with os.fdopen(fd, "rb") as f:
            data = f.read(1 << 16)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(stage)
    try:
        config = json.loads(data)
        if not (isinstance(config, dict) and config.get("base_url") and config.get("token")):
            raise ValueError
    except ValueError:
        return "staged config is not a mount config"
    _write_private(paths.config, data)
    return None


def rebase(base_url: str | None, paths: Paths) -> bool:
    """Point the installed config at ``base_url``, keeping its token: the
    server can move while the token it issued still has time left."""
    if not base_url:
        return False
    with open(paths.config) as f:
        config = json.load(f)
    if config.get("base_url") == base_url:
        return False
    _write_private(paths.config, json.dumps({**config, "base_url": base_url}).encode())
    return True


# --- the daemon ---------------------------------------------------------


def _is_daemon(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f"-m\0{__package__}\0serve\0".encode() in f.read()
    except OSError:
        return False


def retire(state: dict) -> None:
    """Stop the daemon a new one replaced. Where its mount can be unmounted,
    that is enough: the daemon exits once the requests open against it
    finish. Elsewhere it is stopped after ``RETIRE_AFTER_S``, for the same
    reason."""
    old, pid = state.get("mount"), state.get("pid")
    if old and mounted(old):
        subprocess.run(["umount", "-l", old], check=False, capture_output=True)
        if not mounted(old):
            return
    if not _is_daemon(pid):
        return
    # Checked again when the grace ends: the pid may belong to another
    # process by then.
    marker = f"-m {__package__} serve"
    subprocess.Popen(
        [
            "sh",
            "-c",
            f"sleep {RETIRE_AFTER_S}; tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null"
            f" | grep -qF -- '{marker}' && kill {pid}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        start_new_session=True,
        close_fds=True,
    )


def sweep(keep: str, paths: Paths) -> None:
    """Remove the generation directories nothing is mounted on any more."""
    try:
        names = os.listdir(paths.generations)
    except OSError:
        return
    live = set(_mounts())
    for name in names:
        path = os.path.join(paths.generations, name)
        if path != keep and path not in live:
            with contextlib.suppress(OSError):
                os.rmdir(path)


def _drop_copies(keep: str, paths: Paths) -> None:
    """Remove the copies of other code (``boot`` makes them); a replaced
    daemon loaded its modules when it started."""
    with contextlib.suppress(OSError):
        for name in os.listdir(paths.code):
            if name.partition(".")[0] != keep:
                shutil.rmtree(os.path.join(paths.code, name), ignore_errors=True)


def point(generation: str, paths: Paths) -> None:
    """Swap the mount link over in one step, so a path through it never dangles."""
    if os.path.isdir(paths.mount) and not os.path.islink(paths.mount):
        os.rmdir(paths.mount)  # an empty directory in the way; anything more is reported
    swap_link(generation, paths.mount)


def start(
    args, state: dict, paths: Paths, gate: Callable[[], str | None] | None = None
) -> tuple[bool, str | None]:
    """Serve, replacing a daemon that died or runs older code. Updates
    ``state`` on success.

    A new daemon loads while ``gate`` runs and mounts only if it answers
    None; otherwise it exits unmounted, and nothing was started.

    Returns whether a daemon was started, and why it failed if it did.
    """
    code = code_version()
    if healthy(paths.mount) and state.get("code") == code:
        return False, None
    # The copy ``boot`` checked and runs this command from, never the
    # shipped package.
    home = os.path.dirname(_PACKAGE_DIR)
    generation = os.path.join(paths.generations, f"{time.time_ns():x}")
    os.makedirs(generation)
    log = open(paths.log, "ab")
    command = [sys.executable, "-m", __package__, "serve", "--root", args.root, "--mount", generation]
    child = subprocess.Popen(
        [*command, "--gated"] if gate else command,
        stdin=subprocess.PIPE if gate else subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        cwd="/",
        env={**os.environ, "PYTHONPATH": home},
        start_new_session=True,
        close_fds=True,
    )
    if gate is not None:
        refused = gate()
        with contextlib.suppress(OSError):
            if not refused:
                child.stdin.write(b"go\n")
            child.stdin.close()
        if refused:
            with contextlib.suppress(OSError):
                os.rmdir(generation)
            return False, None
    deadline = time.monotonic() + START_TIMEOUT_S
    while not healthy(generation):
        if child.poll() is not None or time.monotonic() > deadline:
            if child.poll() is None:
                child.kill()
            return True, f"the mount did not come up; see {paths.log}"
        time.sleep(0.05)
    try:
        point(generation, paths)
    except OSError as exc:
        child.kill()
        return True, f"could not point {paths.mount} at the new mount: {exc}"
    retire(state)
    sweep(generation, paths)
    _drop_copies(code, paths)
    state.update(pid=child.pid, code=code, mount=generation)
    return True, None
