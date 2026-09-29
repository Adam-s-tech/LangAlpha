"""livefs: the user's server-held files as a directory in the sandbox.

Ships verbatim into ``_internal/src/livefs/`` and runs there as
``python3 -m livefs``. Kept on the host inside ``src/`` so it is linted and
testable like any other module; the host imports only ``protocol``.
"""
