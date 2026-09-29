"""The file panel serves the user's data files through the agent's own routes.

A catalog path reads through its route class, which renders the rows as the
agent reads them and names the panel's refusals. The panel never writes or
deletes one, since that would skip the route's schema and version checks.
Every other path, a README or a file under the other route's folder included,
falls through to the sandbox.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from ptc_agent.agent.backends.automations import AutomationsBackend
from ptc_agent.agent.backends.user_data import UserDataBackend
from ptc_agent.agent.filesystem_routes import route_for
from src.server.app.workspace_files import crud
from src.server.app.workspace_files._shared import _VIRTUAL_FILES, _virtual_file
from src.server.services import user_data_io
from src.server.services.automations import file as automations_file

USER = "user-fake-1"
WORKSPACE_ID = "00000000-0000-4000-8000-00000000aaaa"
WORKSPACE = {
    "workspace_id": WORKSPACE_ID,
    "user_id": USER,
    "status": "running",
    "config": None,
    "sandbox_id": "sb-fake",
}
ROOT = "/home/workspace"
AUTOMATIONS = ".agents/user/automations/automations.json"
PORTFOLIO = ".agents/user/profile/portfolio.json"
CATALOG = {
    PORTFOLIO: UserDataBackend,
    ".agents/user/profile/watchlist.json": UserDataBackend,
    ".agents/user/profile/preference.json": UserDataBackend,
    AUTOMATIONS: AutomationsBackend,
}


def test_each_catalog_file_maps_to_the_route_that_serves_it():
    assert {path: route_for(path) for path in _VIRTUAL_FILES} == CATALOG


@pytest.mark.parametrize(
    "path",
    [
        ".agents/user/profile/README.md",
        ".agents/user/automations/README.md",
        ".agents/user/profile/automations.json",
        ".agents/user/automations/portfolio.json",
        ".agents/user/profile",
        "portfolio.json",
    ],
)
def test_no_other_path_has_a_route(path):
    assert route_for(path) is None
    assert _virtual_file(path) is None


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.setattr(crud, "db_get_workspace", AsyncMock(return_value=WORKSPACE))
    monkeypatch.setattr(crud, "owner_work_dir", lambda _ws: ROOT)


async def _read(path: str) -> dict:
    return await crud.read_workspace_file(
        workspace_id=WORKSPACE_ID, x_user_id=USER, path=path, offset=0, limit=100, unlimited=True
    )


@pytest.mark.asyncio
async def test_the_panel_reads_automations_as_the_agent_does(workspace, monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(automations_file.auto_db, "list_all_automations", fetch)

    result = await _read(AUTOMATIONS)

    assert (result["content"], result["source"]) == ('{\n  "automations": []\n}\n', "automations_backend")
    fetch.assert_awaited_once_with(USER, conn=None)


@pytest.mark.asyncio
async def test_the_panel_reads_a_profile_file_as_the_agent_does(workspace, monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(user_data_io, "fetch_portfolio_for_user", fetch)

    result = await _read(PORTFOLIO)

    assert (result["content"], result["source"]) == ('{\n  "holdings": []\n}', "user_data_backend")
    fetch.assert_awaited_once_with(USER)


@pytest.mark.parametrize(("path", "route"), [(AUTOMATIONS, AutomationsBackend), (PORTFOLIO, UserDataBackend)])
@pytest.mark.asyncio
async def test_a_failed_read_answers_with_its_routes_message(workspace, monkeypatch, path, route):
    down = AsyncMock(side_effect=RuntimeError("database down"))
    monkeypatch.setattr(automations_file.auto_db, "list_all_automations", down)
    monkeypatch.setattr(user_data_io, "fetch_portfolio_for_user", down)

    with pytest.raises(HTTPException) as exc:
        await _read(path)

    assert (exc.value.status_code, exc.value.detail) == (500, route.read_failure)


@pytest.mark.parametrize(("path", "route"), list(CATALOG.items()))
@pytest.mark.asyncio
async def test_the_panel_never_writes_a_data_file(workspace, path, route):
    with pytest.raises(HTTPException) as exc:
        await crud.write_workspace_file(
            workspace_id=WORKSPACE_ID, x_user_id=USER, path=path, body=crud.WriteFileRequest(content="{}")
        )

    assert (exc.value.status_code, exc.value.detail) == (400, route.read_only)


@pytest.mark.asyncio
async def test_the_panel_never_deletes_a_data_file(workspace, monkeypatch):
    sandbox = MagicMock()
    sandbox.validate_path.return_value = True
    sandbox.execute_bash_command = AsyncMock(side_effect=AssertionError("a data file reached rm"))

    @asynccontextmanager
    async def _acquire(*_):
        yield sandbox, WORKSPACE

    monkeypatch.setattr(crud, "_acquire_sandbox_to_change", _acquire)
    monkeypatch.setattr(
        crud, "contained_sandbox_paths", AsyncMock(side_effect=lambda _sb, paths, work_dir: paths)
    )

    result = await crud.delete_workspace_files(
        workspace_id=WORKSPACE_ID, x_user_id=USER, body=crud.DeleteFilesRequest(paths=list(CATALOG))
    )

    assert result == {
        "deleted": [],
        "errors": [{"path": path, "detail": route.undeletable} for path, route in CATALOG.items()],
    }
