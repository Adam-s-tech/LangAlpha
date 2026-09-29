"""The file operations the mount answers, each a request to the server.

A command sees the files as they were when it first touched them, plus its
own and its siblings' saves through this mount: its view is keyed by the call
id the tool put in the command's environment, so the next command starts
fresh without the daemon polling anything.

Two things outlive a command without showing it anything stale. The
directories the server makes up (the root, ``workspaces``, each workspace,
``user``) change only when the host runs ``up`` again, which rewrites the
daemon's state file. File bytes are kept by version, and a command still takes
each file's version from a listing of its own.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import NamedTuple

from .protocol import CALL_ENV, MAX_FILE_BYTES, PROVISIONAL_HEADER, Refusal, code_of
from .remote import Remote, error_for, fail

_CALL_ENV = CALL_ENV.encode() + b"="
VIEW_LIMIT = 32
UNTAGGED_TTL_S = 2.0
PID_TTL_S = 2.0
#: How long a made-up directory's listing answers without being asked again:
#: a backstop for a layout change no ``up`` followed.
STRUCTURE_TTL_S = 300.0
#: The file bytes kept, at most; less where the sandbox has little memory.
CONTENT_BYTES = 64 * 1024 * 1024

_GONE = (Refusal.NOT_FOUND, Refusal.NOT_DIRECTORY)


def call_in_environ(pid: int, proc: str = "/proc") -> str | None:
    """The call id a process started with. Its starting environment, not its
    current one: children inherit it, and the shell's own redirections are
    requests of the shell's pid."""
    try:
        with open(f"{proc}/{pid}/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if item.startswith(_CALL_ENV):
                    return item[len(_CALL_ENV):].decode(errors="replace") or None
    except OSError:
        pass
    return None


def content_budget(default: int = CONTENT_BYTES, cgroup: str = "/sys/fs/cgroup") -> int:
    """``default``, or a sixteenth of the sandbox's memory limit when smaller."""
    for name in ("memory.max", "memory/memory.limit_in_bytes"):
        try:
            with open(os.path.join(cgroup, name)) as f:
                limit = f.read().strip()
        except OSError:
            continue
        return min(default, int(limit) // 16) if limit.isdigit() else default
    return default


class View:
    """What one command has seen: directory listings, and the version of each
    file it read or saved. The bytes are the daemon's (``_Contents``), so a
    command that reads much pins none of it."""

    __slots__ = ("listings", "creatable", "versions", "kept", "born")

    def __init__(self) -> None:
        self.listings: dict[str, dict[str, dict]] = {}
        self.creatable: dict[str, bool] = {}
        self.versions: dict[str, str] = {}
        #: Directories above a file this command removed. The server drops a
        #: directory with its last file, and `rm -r` then rmdirs it.
        self.kept: set[str] = set()
        self.born = time.monotonic()


def _exact(version: str | None, data: bytes) -> bool:
    """Whether ``version`` names these bytes and no others. Hashed outside
    the daemon's lock: a large file takes milliseconds."""
    return bool(version) and len(version) >= 16 and (
        hashlib.sha256(data).hexdigest().startswith(version)
    )


class _Contents:
    """File bytes by (path, version), bounded by their total size; the one
    used longest ago goes first.

    Bytes whose version is their digest are any command's. A route may keep
    a version while its content moves (the automations file's run state), so
    other bytes are kept per command too, and one command's never replace or
    stand for another's.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._items: OrderedDict[tuple[str, str, View | None], bytes] = OrderedDict()
        self._paths: dict[str, set[tuple[str, str, View | None]]] = {}
        self._size = 0

    def get(self, path: str, version: str | None, view: View) -> bytes | None:
        if not version:
            return None
        for key in ((path, version, None), (path, version, view)):
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
                return data
        return None

    def put(
        self, path: str, version: str | None, data: bytes, owner: View | None, exact: bool
    ) -> None:
        if not version or len(data) > self.limit or not (exact or owner is not None):
            return
        key = (path, version, None if exact else owner)
        self._discard(key)
        self._items[key] = data
        self._paths.setdefault(path, set()).add(key)
        self._size += len(data)
        while self._size > self.limit:
            self._discard(next(iter(self._items)))

    def drop(self, path: str) -> None:
        """Forget every version of the file, whoever read it."""
        for key in list(self._paths.get(path, ())):
            self._discard(key)

    def _discard(self, key: tuple[str, str, View | None]) -> None:
        data = self._items.pop(key, None)
        if data is None:
            return
        self._size -= len(data)
        keys = self._paths[key[0]]
        keys.discard(key)
        if not keys:
            del self._paths[key[0]]


class _Made(NamedTuple):
    """A made-up directory's listing, which every command may look in."""

    entries: dict[str, dict]
    creatable: bool
    until: float
    #: Names a command asked for that its own listing of it lacked.
    absent: set[str]


class Handle:
    __slots__ = (
        "path",
        "buf",
        "base",
        "dirty",
        "written",
        "append",
        "call",
        "writable",
        "writing",
        "unsure",
        "refused",
        "rev",
        "lock",
        "saving",
    )

    def __init__(
        self, path, buf, base, *, dirty, append, call, writable, writing=True
    ) -> None:
        self.path = path
        #: Bytes until the first write or resize, so a read copies nothing.
        self.buf: bytes | bytearray = buf
        self.base = base
        self.dirty = dirty
        #: Written or resized since open, not only created or truncated by it.
        self.written = False
        self.append = append
        self.call = call
        self.writable = writable
        #: Opened to write: only such a handle holds bytes the server has not.
        self.writing = writing
        #: Its last save failed with no answer, so it may have landed.
        self.unsure = False
        #: Grown past the size limit once, so ``buf`` holds only part of what
        #: the program meant to write: nothing from this handle is saved.
        self.refused = False
        #: Moved by every change to ``buf``: a save answered after a later
        #: write leaves the handle dirty, so that write is saved too.
        self.rev = 0
        #: ``lock`` guards ``buf`` and ``rev``; ``saving`` lets one save of
        #: the handle run at a time without holding writes for the request.
        self.lock = threading.Lock()
        self.saving = threading.Lock()

    def mutable(self) -> bytearray:
        if not isinstance(self.buf, bytearray):
            self.buf = bytearray(self.buf)
        return self.buf


def split(path: str) -> tuple[str, str]:
    parent, _, name = path.rpartition("/")
    return parent or "/", name


def _above(path: str) -> list[str]:
    """The directories above ``path``, nearest first, "/" last."""
    found = []
    while path != "/":
        path = split(path)[0]
        found.append(path)
    return found


class LiveFS:
    """The mfusepy operations. mfusepy registers the methods it finds by
    name, so every public method here is a FUSE callback.

    ``caller`` names the pid of the process a request comes from, and
    ``call_of`` the call that pid runs for. ``layout`` is the state file
    ``up`` rewrites whenever the computer's workspaces or links may have
    changed.
    """

    use_ns = True

    def __init__(
        self,
        remote: Remote,
        uid: int,
        gid: int,
        *,
        caller: Callable[[], int],
        call_of: Callable[[int], str | None],
        layout: str | None = None,
        content_bytes: int = CONTENT_BYTES,
    ) -> None:
        self._remote = remote
        self._uid = uid
        self._gid = gid
        self._caller = caller
        self._call_of = call_of
        self._lock = threading.RLock()
        self._views: OrderedDict[str | None, View] = OrderedDict()
        self._pids: dict[int, tuple[str | None, float]] = {}
        self._handles: dict[int, Handle] = {}
        self._next_fh = 1
        #: Directories made here and not yet holding a saved file. Daemon-wide,
        #: since an empty directory has to outlast the command that made it.
        self._local_dirs: set[str] = set()
        self._mtimes: dict[str, tuple[str, int]] = {}
        self._started = time.time_ns()
        self._layout = layout
        self._layout_seen: object = None
        self._sources: list[str] | None = None
        self._made: dict[str, _Made] = {}
        #: Directories each made-up directory holds, as the links ``up`` made
        #: through them say: only ever a reason to look no further.
        self._seeded: dict[str, set[str]] = {}
        self._bytes = _Contents(content_bytes)

    # --- whose view -----------------------------------------------------

    def _call(self) -> str | None:
        pid = self._caller()
        now = time.monotonic()
        with self._lock:
            hit = self._pids.get(pid)
            if hit and hit[1] > now:
                return hit[0]
        call = self._call_of(pid)
        with self._lock:
            if len(self._pids) > 4096:
                self._pids.clear()
            self._pids[pid] = (call, now + PID_TTL_S)
        return call

    def _view(self, call: str | None) -> View:
        with self._lock:
            view = self._views.get(call)
            stale = (
                view is not None
                and call is None
                and time.monotonic() - view.born > UNTAGGED_TTL_S
            )
            if view is None or stale:
                self._check_layout()
                view = View()
                self._views[call] = view
                while len(self._views) > VIEW_LIMIT:
                    self._views.popitem(last=False)
            else:
                self._views.move_to_end(call)
            return view

    def _here(self) -> tuple[str | None, View]:
        call = self._call()
        return call, self._view(call)

    def _check_layout(self) -> None:
        """Forget the made-up directories once ``up`` has linked a different
        set of mount paths, which it does whenever the computer's workspaces
        or layout change. Checked as each command starts, so no command's
        walk spans two layouts; a rewrite that changed no link (a new token)
        keeps them."""
        if self._layout is None:
            return
        try:
            info = os.stat(self._layout)
            seen: object = (info.st_ino, info.st_mtime_ns, info.st_size)
        except OSError:
            seen = None
        if seen == self._layout_seen:
            return
        self._layout_seen = seen
        try:
            with open(self._layout) as f:
                sources = json.load(f).get("sources")
        except (OSError, ValueError, AttributeError):
            sources = None
        if sources is not None and sources == self._sources:
            return
        self._sources = sources
        self._made.clear()
        self._seeded = {}
        for source in sources or ():
            parent = "/"
            for name in [p for p in source.split("/") if p][:-1]:
                self._seeded.setdefault(parent, set()).add(name)
                parent = f"{parent.rstrip('/')}/{name}"

    def _is_local(self, path: str, view: View | None) -> bool:
        return path in self._local_dirs or (view is not None and path in view.kept)

    def _refused(self, path: str, status: int, data: bytes, view: View | None = None) -> OSError:
        """The error a refusal reaches the program as. A path found gone
        below a made-up directory may mean that directory changed, so it and
        those above it are asked again; below a directory kept only here, the
        server holding nothing is expected."""
        if status == 404 or code_of(data) in _GONE:
            with self._lock:
                chain = [path, *_above(path)]
                if not any(self._is_local(p, view) for p in chain):
                    for p in chain:
                        self._made.pop(p, None)
                        self._seeded.pop(p, None)
        return error_for(status, data)

    # --- server reads ---------------------------------------------------

    def _listing(
        self, path: str, view: View, call: str | None, name: str | None = None
    ) -> dict[str, dict]:
        """A directory's entries as this command sees them.

        A made-up directory's listing, taken by any command, answers for a
        ``name`` it holds; a name it lacks is asked again, so a workspace
        added since is never hidden, and the fresh answer is this command's.
        A name still missing then answers as missing until the listing goes:
        a search looks for the same ignore files in every directory above
        the one it searches, which would list them all again each time.
        """
        with self._lock:
            entries = view.listings.get(path)
            if entries is not None:
                self._note_absent(path, name, entries)
                return entries
            made = self._made.get(path)
            if made is not None:
                if made.until <= time.monotonic():
                    del self._made[path]
                elif name is None or name in made.entries or name in made.absent:
                    return made.entries
            if name is not None and name in self._seeded.get(path, ()):
                return {name: {"name": name, "type": "dir"}}
        status, _, data = self._remote.request(
            "GET", "list", {"path": path.lstrip("/")}, call=call
        )
        if status != 200:
            raise self._refused(path, status, data, view)
        listing = json.loads(data)
        base = path.rstrip("/")
        entries, inline = {}, []
        for entry in listing["entries"]:
            content = entry.pop("content", None)
            if isinstance(content, str):
                encoded = content.encode()
                if len(encoded) == entry.get("size"):
                    version = entry.get("version")
                    inline.append((entry["name"], version, encoded, _exact(version, encoded)))
            entries[entry["name"]] = entry
        with self._lock:
            for filename, version, encoded, exact in inline:
                self._bytes.put(f"{base}/{filename}", version, encoded, view, exact)
            creatable = bool(listing.get("writable"))
            view.listings[path] = entries
            view.creatable[path] = creatable
            if path in self._seeded:
                self._seeded[path].intersection_update(entries)
            # One holding a file is this command's alone: a file's version
            # moves with no ``up``.
            if listing.get("structural") and all(e.get("type") == "dir" for e in entries.values()):
                before = self._made.get(path)
                self._made[path] = _Made(
                    dict(entries),
                    creatable,
                    time.monotonic() + STRUCTURE_TTL_S,
                    before.absent - entries.keys() if before else set(),
                )
            self._note_absent(path, name, entries)
        return entries

    def _note_absent(self, path: str, name: str | None, entries: dict[str, dict]) -> None:
        """Remember a name this command's own listing of a made-up
        directory lacks. Caller holds the lock."""
        made = self._made.get(path)
        if made is not None and name is not None and name not in entries:
            made.absent.add(name)

    def _on_server(self, path: str, view: View, call: str | None) -> bool:
        """Whether the server holds files under the directory. A directory
        kept here may also still be there, and then the server's rules for
        it apply, not the local ones."""
        try:
            self._listing(path, view, call)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return False
            raise
        return True

    def _creatable(self, path: str, view: View) -> bool:
        with self._lock:
            if path in view.creatable:
                return view.creatable[path]
            made = self._made.get(path)
            return made.creatable if made is not None else True

    def _require_creatable(self, directory: str) -> None:
        """Refuse at create, where the caller sees it; a refusal at close
        is one most programs never check."""
        call, view = self._here()
        if not self._on_server(directory, view, call):
            if self._is_local(directory, view):
                return
            raise fail(errno.ENOENT)
        if not self._creatable(directory, view):
            raise fail(errno.EACCES)

    def _entry(self, path: str, view: View, call: str | None) -> dict | None:
        if path == "/":
            return {"type": "dir"}
        parent, name = split(path)
        try:
            entries = self._listing(parent, view, call, name)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return None
            raise
        return entries.get(name)

    def _held(self, path: str, version: str, call: str | None) -> bytes | None:
        """That version's bytes, from a handle the command still has open on
        it: another command's may hold other bytes under the same version."""
        for handle in self._handles.values():
            if handle.path != path or handle.call != call:
                continue
            with handle.lock:
                if handle.base == version and not (handle.dirty or handle.written):
                    return bytes(handle.buf)
        return None

    def _content(
        self, path: str, view: View, call: str | None, version: str | None
    ) -> tuple[bytes, str]:
        """The file's bytes as this command sees them: the version it read
        or saved, else ``version``, the one its listing names.

        The daemon may have let a version's bytes go since this command read
        them. Fetched again as another version, the file changed after this
        command read it, which it cannot be shown as if it had not.
        """
        with self._lock:
            pinned = view.versions.get(path)
            wanted = pinned or version
            data = self._bytes.get(path, wanted, view)
            if data is None and pinned:
                data = self._held(path, pinned, call)
            if data is not None:
                view.versions[path] = wanted
                return data, wanted
        status, resp, data = self._remote.request(
            "GET", "read", {"path": path.lstrip("/")}, call=call
        )
        if status != 200:
            raise self._refused(path, status, data, view)
        got = resp.getheader("ETag", "").strip('"')
        # A version that is not a digest can come back with other bytes;
        # those are shown, as they would be had nothing been let go.
        if pinned and got != pinned:
            raise fail(errno.ESTALE)
        exact = _exact(got, data)
        with self._lock:
            self._bytes.put(path, got, data, view, exact)
            if got:
                view.versions[path] = got
        return data, got

    # --- attributes -----------------------------------------------------

    def _mtime(self, path: str, version: str) -> int:
        with self._lock:
            seen = self._mtimes.get(path)
            if seen is None or seen[0] != version:
                seen = (version, time.time_ns() if seen else self._started)
                self._mtimes[path] = seen
            return seen[1]

    def _dir_attrs(self) -> dict:
        return {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_nlink": 2,
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_mtime": self._started,
            "st_ctime": self._started,
            "st_atime": self._started,
        }

    def _file_attrs(self, size: int, writable: bool, mtime: int) -> dict:
        return {
            "st_mode": stat.S_IFREG | (0o644 if writable else 0o444),
            "st_nlink": 1,
            "st_size": size,
            "st_blocks": (size + 511) // 512,
            "st_blksize": 4096,
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_mtime": mtime,
            "st_ctime": mtime,
            "st_atime": mtime,
        }

    def _pending(self, path: str) -> Handle | None:
        """An open handle whose bytes are newer than the server's."""
        with self._lock:
            for handle in self._handles.values():
                if (
                    handle.path == path
                    and handle.writing
                    and (handle.dirty or handle.base is None)
                ):
                    return handle
        return None

    def getattr(self, path, fh=None):
        handle = self._handles.get(fh) if fh else None
        handle = handle or self._pending(path)
        if handle is not None:
            return self._file_attrs(len(handle.buf), True, time.time_ns())
        if path in self._local_dirs:
            return self._dir_attrs()
        call, view = self._here()
        if path in view.kept:
            return self._dir_attrs()
        entry = self._entry(path, view, call)
        if entry is None:
            raise fail(errno.ENOENT)
        if entry["type"] == "dir":
            return self._dir_attrs()
        return self._file_attrs(
            entry["size"],
            entry.get("writable", True),
            self._mtime(path, entry["version"]),
        )

    def readdir(self, path, fh):
        call, view = self._here()
        try:
            names = set(self._listing(path, view, call))
        except OSError as exc:
            if exc.errno != errno.ENOENT or not self._is_local(path, view):
                raise
            names = set()
        with self._lock:
            names |= {
                split(d)[1] for d in self._local_dirs | view.kept if split(d)[0] == path
            }
            names |= {
                split(h.path)[1]
                for h in self._handles.values()
                if h.writing and h.base is None and split(h.path)[0] == path
            }
        return [".", "..", *sorted(names)]

    # --- files ----------------------------------------------------------

    def _add(self, handle: Handle) -> int:
        with self._lock:
            fh = self._next_fh
            self._next_fh += 1
            self._handles[fh] = handle
            return fh

    def open(self, path, flags):
        writing = (flags & os.O_ACCMODE) != os.O_RDONLY
        if writing:
            # A handle that made or emptied the file saves first, so this one
            # starts from the file it made.
            self._settle(path)
        else:
            pending = self._pending(path)
            if pending is not None:
                # What the writer holds so far, as a disk would show it.
                with pending.lock:
                    held = bytes(pending.buf)
                return self._add(
                    Handle(
                        path,
                        held,
                        None,
                        dirty=False,
                        append=False,
                        call=self._call(),
                        writable=True,
                        writing=False,
                    )
                )
        call, view = self._here()
        entry = self._entry(path, view, call)
        if entry is None:
            raise fail(errno.ENOENT)
        if entry["type"] == "dir":
            raise fail(errno.EISDIR)
        writable = entry.get("writable", True)
        if writing and not writable:
            raise fail(errno.EACCES)
        if writing and flags & os.O_TRUNC:
            buf, base = b"", entry["version"]
        else:
            buf, base = self._content(path, view, call, entry.get("version"))
        return self._add(
            Handle(
                path,
                buf,
                base,
                dirty=bool(writing and flags & os.O_TRUNC),
                append=bool(flags & os.O_APPEND),
                call=call,
                writable=writable,
                writing=writing,
            )
        )

    def create(self, path, mode, flags=0):
        parent, _ = split(path)
        if self.getattr(parent)["st_mode"] & stat.S_IFDIR == 0:
            raise fail(errno.ENOTDIR)
        self._require_creatable(parent)
        return self._add(
            Handle(
                path,
                b"",
                None,
                dirty=True,
                append=bool(flags & os.O_APPEND),
                call=self._call(),
                writable=True,
            )
        )

    def read(self, path, size, offset, fh):
        buf = self._handles[fh].buf
        if isinstance(buf, bytes):
            return buf[offset : offset + size]
        return bytes(buf[offset : offset + size])

    def write(self, path, data, offset, fh):
        handle = self._handles[fh]
        with handle.lock:
            buf = handle.mutable()
            if handle.append:
                offset = len(buf)
            end = offset + len(data)
            if end > MAX_FILE_BYTES:
                handle.refused = True
                raise fail(errno.EFBIG)
            if offset > len(buf):
                buf.extend(b"\0" * (offset - len(buf)))
            buf[offset:end] = data
            handle.dirty = handle.written = True
            handle.rev += 1
        return len(data)

    def truncate(self, path, length, fh=None):
        if length > MAX_FILE_BYTES:
            if fh is not None:
                self._handles[fh].refused = True
            raise fail(errno.EFBIG)
        if fh is not None:
            self._resize(self._handles[fh], length)
            return 0
        fh = self.open(path, os.O_WRONLY)
        try:
            handle = self._handles[fh]
            self._resize(handle, length)
            self._save(handle)
        finally:
            self.release(path, fh)
        return 0

    @staticmethod
    def _resize(handle: Handle, length: int) -> None:
        with handle.lock:
            buf = handle.mutable()
            if length < len(buf):
                del buf[length:]
            else:
                buf.extend(b"\0" * (length - len(buf)))
            handle.dirty = handle.written = True
            handle.rev += 1

    def flush(self, path, fh):
        # Every close() flushes, including the shell's close of the fd it
        # has just dup2'd for `> file`, before anything is written. A handle
        # that has only made or emptied its file saves at release instead,
        # or once another operation on the path needs the server to hold it
        # (``_settle``), so the bytes the command then writes make one save.
        handle = self._handles.get(fh)
        if handle is not None and handle.dirty and handle.written:
            self._save(handle)
        return 0

    def fsync(self, path, datasync, fh):
        handle = self._handles.get(fh)
        if handle is not None and handle.dirty:
            self._save(handle)
        return 0

    def release(self, path, fh):
        handle = self._handles.get(fh)
        try:
            if handle is not None and handle.dirty:
                self._save(handle)
        finally:
            with self._lock:
                self._handles.pop(fh, None)
        return 0

    def _settle(self, path: str) -> None:
        """Send the saves ``flush`` left for release on ``path``, for an
        operation the server must see the made or emptied file for first.
        Provisional, since bytes may still follow on those handles: a refusal
        is not reported, and release sends it again if none did."""
        with self._lock:
            waiting = [
                h
                for h in self._handles.values()
                if h.path == path and h.writing and h.dirty and not h.written
            ]
        for handle in waiting:
            with contextlib.suppress(OSError):
                self._save(handle, provisional=True)

    def _save(self, handle: Handle, *, provisional: bool = False) -> None:
        # A write may land while the request is out: the bytes sent are
        # taken at ``rev``, and only an answer for the latest one leaves the
        # handle clean.
        with handle.saving:
            with handle.lock:
                if not handle.dirty:
                    return  # the save this one waited for sent it all
                if handle.refused:
                    # Failed once, like a refusal the server reports.
                    handle.dirty = False
                    raise fail(errno.EFBIG)
                body, rev = bytes(handle.buf), handle.rev
            if handle.unsure and self._landed(handle, body, rev):
                return
            headers = (
                {"If-Match": f'"{handle.base}"'}
                if handle.base
                else {"If-None-Match": "*"}
            )
            if provisional:
                headers[PROVISIONAL_HEADER] = "1"
            try:
                status, _, data = self._remote.request(
                    "PUT",
                    "write",
                    {"path": handle.path.lstrip("/")},
                    body=body,
                    headers=headers,
                    call=self._call() or handle.call,
                )
            except OSError:
                # It may have landed, and sent again its precondition would
                # refuse it as a conflict: the next try asks first.
                handle.unsure = True
                raise
            handle.unsure = False
            if status != 200:
                # A reported refusal is not retried at the next flush or
                # release, which would report it twice.
                with handle.lock:
                    if handle.rev == rev:
                        handle.dirty = provisional
                raise self._refused(handle.path, status, data)
            saved = json.loads(data)
            self._saved(
                handle, body, rev, saved["version"], saved["size"], bool(saved.get("as_sent"))
            )

    def _landed(self, handle: Handle, body: bytes, rev: int) -> bool:
        """Whether the server holds exactly what a save with no answer sent.
        Only a save stored byte for byte is recognized: one the route
        rewrote reads back otherwise and is reported not saved."""
        status, resp, data = self._remote.request(
            "GET", "read", {"path": handle.path.lstrip("/")}, call=self._call() or handle.call
        )
        if status != 200 or data != body:
            return False
        self._saved(handle, body, rev, resp.getheader("ETag", "").strip('"'), len(body), True)
        return True

    def _saved(
        self, handle: Handle, body: bytes, rev: int, version: str, size: int, as_sent: bool
    ) -> None:
        with handle.lock:
            handle.base = version
            handle.unsure = False
            if handle.rev == rev:
                handle.dirty = False
        entry = {
            "name": split(handle.path)[1],
            "type": "file",
            "size": size,
            "version": version,
            "writable": True,
        }
        # The server holds the directories above a saved file, and drops
        # them again once the last file inside goes.
        above = set(_above(handle.path)) - {"/"}
        exact = as_sent and _exact(version, body)
        with self._lock:
            self._local_dirs -= above
            if as_sent:
                # For any command when the version names these bytes, else
                # for the one that saved them.
                owner = None if exact else self._views.get(handle.call)
                self._bytes.put(handle.path, version, body, owner, exact)
            else:
                # Stored otherwise than sent, under a version that may be
                # one already kept with other bytes.
                self._bytes.drop(handle.path)
            for view in self._views.values():
                view.kept -= above
                self._placed(view, handle.path, entry)
                if as_sent:
                    view.versions[handle.path] = version
                else:
                    view.versions.pop(handle.path, None)

    @staticmethod
    def _placed(view: View, path: str, entry: dict | None) -> None:
        """Show a file the server now holds in ``view``'s listing of its
        directory (asked again when its ``entry`` is not known), and each
        directory above it in the listing above that, which a listing taken
        before the directory was made lacks."""
        parent, name = split(path)
        listing = view.listings.get(parent)
        if listing is not None:
            if entry is None:
                del view.listings[parent]
            else:
                listing[name] = entry
        child = parent
        while child != "/":
            directory, dirname = split(child)
            listing = view.listings.get(directory)
            if listing is not None and dirname not in listing:
                listing[dirname] = {"name": dirname, "type": "dir"}
            child = directory

    def _removed(self, path: str, seen: View) -> None:
        """Take a file the server no longer holds out of every view's
        listing. When its directory held nothing else, as ``seen`` (the
        remover's view) listed it, the server dropped that too, so each view
        asks again for the listings from there up. The made-up directories
        stay: no file is ever directly inside one."""
        parent, name = split(path)
        with self._lock:
            # A file made there again may be given a version seen before.
            self._bytes.drop(path)
            listing = seen.listings.get(parent)
            emptied = listing is None or listing.keys() <= {name}
            for view in self._views.values():
                view.versions.pop(path, None)
                listing = view.listings.get(parent)
                if listing is not None:
                    listing.pop(name, None)
                if emptied:
                    for directory in (parent, *_above(parent)):
                        view.listings.pop(directory, None)

    def _keep_parents(self, path: str, view: View) -> None:
        """Removing the last file of a directory must not make the directory
        vanish under a caller about to rmdir it, as `rm -r` is."""
        parent = split(path)[0]
        with self._lock:
            while parent != "/":
                view.kept.add(parent)
                parent = split(parent)[0]

    def unlink(self, path):
        self._settle(path)
        call, view = self._here()
        status, _, data = self._remote.request(
            "POST", "delete", {"path": path.lstrip("/")}, call=call
        )
        if status != 204:
            raise self._refused(path, status, data, view)
        self._keep_parents(path, view)
        self._removed(path, view)
        return 0

    def rename(self, old, new):
        call, view = self._here()
        if self._is_local(old, view) and not self._on_server(old, view, call):
            if self._exists(new):
                raise fail(errno.EEXIST)
            with self._lock:
                for dirs in (self._local_dirs, view.kept):
                    moved = {d for d in dirs if d == old or d.startswith(old + "/")}
                    dirs.difference_update(moved)
                    dirs.update(new + d[len(old):] for d in moved)
            return 0
        self._settle(old)
        self._settle(new)
        status, _, data = self._remote.request(
            "POST",
            "rename",
            {"path": old.lstrip("/"), "to": new.lstrip("/")},
            call=call,
        )
        if code_of(data) == Refusal.IS_DIRECTORY:
            # mv answers a cross-device rename by copying file by file.
            raise fail(errno.EXDEV)
        if status != 204:
            raise self._refused(old, status, data, view)
        self._keep_parents(old, view)
        self._removed(old, view)
        with self._lock:
            self._bytes.drop(new)
            for each in self._views.values():
                each.versions.pop(new, None)
                self._placed(each, new, None)
        return 0

    # --- directories ----------------------------------------------------

    def _exists(self, path: str) -> bool:
        try:
            self.getattr(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise
        return True

    def mkdir(self, path, mode):
        if self._exists(path):
            raise fail(errno.EEXIST)
        parent, _ = split(path)
        if self.getattr(parent)["st_mode"] & stat.S_IFDIR == 0:
            raise fail(errno.ENOTDIR)
        self._require_creatable(parent)
        # Kept here until a file is saved inside it: the server holds
        # files, and a directory is only the path they share.
        with self._lock:
            self._local_dirs.add(path)
        return 0

    def rmdir(self, path):
        names = [n for n in self.readdir(path, 0) if n not in (".", "..")]
        if names:
            raise fail(errno.ENOTEMPTY)
        _, view = self._here()
        if self._is_local(path, view):
            parent, name = split(path)
            with self._lock:
                self._local_dirs.discard(path)
                view.kept.discard(path)
                for each in self._views.values():
                    each.listings.pop(path, None)
                    listing = each.listings.get(parent)
                    if listing is not None:
                        listing.pop(name, None)
            return 0
        raise fail(errno.EBUSY)

    # --- accepted and ignored -------------------------------------------

    def chmod(self, path, mode):
        return 0

    def chown(self, path, uid, gid):
        return 0

    def utimens(self, path, times=None):
        return 0

    def statfs(self, path):
        return {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": 1 << 18,
            "f_bfree": 1 << 17,
            "f_bavail": 1 << 17,
            "f_files": 1 << 16,
            "f_ffree": 1 << 15,
            "f_favail": 1 << 15,
            "f_namemax": 255,
        }
