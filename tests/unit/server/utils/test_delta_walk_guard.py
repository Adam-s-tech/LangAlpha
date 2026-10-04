"""Whether the installed Postgres saver still needs the delta walk guard.

``src/server/utils/checkpointer.py`` wraps the saver's history walk around a
bug fixed upstream in langchain-ai/langgraph#8556 but not yet released. This
asks the saver's own walk, with the guard peeled off, the one question the bug
answers wrong, so the upgrade that brings the fix fails here and says what to
remove. The guard's tests live in ``tests/integration/test_delta_history_paging.py``.
"""

import inspect
from importlib.metadata import version

import pytest
from langgraph.checkpoint.postgres.base import BasePostgresSaver

_CHANNELS = ["messages"]
_RECHECK = (
    "re-check whether the guard in src/server/utils/checkpointer.py is still "
    "needed, and teach this test the new shape if it is."
)


def _walk_args(walk) -> dict:
    """The walk state the saver's history read starts from, before any page
    has reached the target."""
    named = {
        "target_id": "target",
        "channels": _CHANNELS,
        "parent_of": {},
        "chain_by_ch": {ch: [] for ch in _CHANNELS},
        "seed_ver_by_ch": {ch: None for ch in _CHANNELS},
        "seed_inline_by_ch": {},
        "walk_cursor_by_ch": {},
        "seeded": set(),
    }
    params = list(inspect.signature(walk).parameters)
    # The guard reads these three positionally.
    if params[:3] != ["target_id", "channels", "parent_of"]:
        pytest.fail(f"The saver's walk now starts {params[:3]}: {_RECHECK}")
    args = {}
    for name in params:
        if name.endswith("_by_i_by_cid"):
            args[name] = [{} for _ in _CHANNELS]
        elif name in named:
            args[name] = named[name]
        else:
            pytest.fail(f"The saver's walk takes a new argument {name!r}: {_RECHECK}")
    return args


def test_the_installed_saver_still_needs_the_walk_guard():
    walk = inspect.unwrap(BasePostgresSaver._try_advance_walks)
    args = _walk_args(walk)

    walk(**args)

    # The bug: a target the first page has not reached is recorded as a root,
    # and the walk never resumes once a later page loads it.
    assert args["walk_cursor_by_ch"] == {"messages": None}, (
        f"langgraph-checkpoint-postgres {version('langgraph-checkpoint-postgres')} "
        "carries the fix for langchain-ai/langgraph#8556, so the guard is no "
        "longer needed. Remove _walk_delta_history_from_loaded_target from "
        "src/server/utils/checkpointer.py and this test, drop the guard import "
        "in tests/integration/test_delta_history_paging.py, and raise the floor "
        "in pyproject.toml to this version."
    )
