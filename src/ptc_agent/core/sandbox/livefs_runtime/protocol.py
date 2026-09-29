"""Names the host and the sandbox daemon both use.

Stdlib only: it ships into the sandbox with the daemon, and the endpoint, the
tools and the mount service import it on the host.
"""

import json
from enum import StrEnum

#: The package directory under ``_internal/src/``, run as ``python3 -m livefs``.
PACKAGE_NAME = "livefs"

#: Where the files are served. Outside the computer root, so nothing walking
#: the root meets it; the paths the agent uses are symlinks into it, which
#: walkers, ``rm -r`` and backups leave alone unless told to follow them.
MOUNT = "/mnt/livefs"

#: The server's endpoint; each action is a path under it (``/list``, ``/write``).
PREFIX = "/api/v1/livefs"

#: The tool puts its call id here, which is how a save reaches the result of
#: the tool call whose command made it.
CALL_ENV = "LIVEFS_CALL_ID"

#: The call id of the process a request is for, read from its ``CALL_ENV``.
CALL_HEADER = "X-Livefs-Call"

#: Marks a save of bytes nobody has written yet, as a shell's `> file` makes
#: before its command writes; the server does not report its refusal.
PROVISIONAL_HEADER = "X-Livefs-Provisional"

#: The most one file served here holds. The server refuses a larger body, so
#: the daemon refuses the write that would grow a file past it, where the
#: program sees it, rather than at close, which most programs never check.
MAX_FILE_BYTES = 256 * 1024

#: A listing may carry a small file's text as its entry's ``content``, the
#: same bytes ``/read`` answers for that version, so reading it costs no
#: request of its own. Up to this much per file (as UTF-8) ...
INLINE_MAX_BYTES = 16 * 1024
#: ... and this much per listing; past it, entries leave it out.
INLINE_LISTING_MAX_BYTES = 128 * 1024


class Refusal(StrEnum):
    """The ``code`` of a refused request, which the daemon answers as an errno."""

    NOT_FOUND = "not_found"
    IS_DIRECTORY = "is_directory"
    NOT_DIRECTORY = "not_directory"
    EXISTS = "exists"
    CHANGED = "changed"
    READ_ONLY = "read_only"
    INVALID = "invalid"
    PRECONDITION_REQUIRED = "precondition_required"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"


def code_of(body: bytes) -> str | None:
    """A refusal's ``code``; None for a body that is not one, such as a
    proxy's error page."""
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    return answer.get("code") if isinstance(answer, dict) else None


class MountError(StrEnum):
    """Why the mount is not serving. ``up`` and ``start`` answer all but the
    last two, which the host adds."""

    NO_CONFIG = "no_config"
    BAD_CONFIG = "bad_config"
    #: Not root, or no /dev/fuse: fixed for the sandbox's life.
    UNSUPPORTED = "unsupported"
    #: libfuse is being installed, so asking again soon may serve.
    INSTALLING = "installing"
    UNREACHABLE = "unreachable"
    START_FAILED = "start_failed"
    LINK_FAILED = "link_failed"
    #: ``start`` found other code shipped than the host expects: an asset
    #: sync is still replacing it, so ``up`` starts the daemon once it lands.
    STALE_CODE = "stale_code"
    #: ``up`` printed nothing the host could read.
    UNANSWERED = "unanswered"
    #: The computer's folders were moving, so ``up`` was not asked.
    BUSY = "busy"
