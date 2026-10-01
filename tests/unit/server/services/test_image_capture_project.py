"""Finalization runs outside the task that binds the PTC project's context."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ptc_agent.config.core import FilesystemConfig
from ptc_agent.core.project_context import ProjectContext, current_project
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.server.database.workspace_folders import WorkspaceFolderMoving
from src.server.services.persistence.image_capture import capture_and_rewrite_images

_HOLD = 'src.server.database.workspace_folders.workspace_folder_in_use'


def _held(events, *, moving=False):
    @asynccontextmanager
    async def hold(_workspace_id):
        if moving:
            raise WorkspaceFolderMoving(_workspace_id)
        events.append('hold')
        try:
            yield
        finally:
            events.append('release')
    return hold


def _chunk():
    return [{'event': 'message_chunk', 'data': {
        'content_type': 'text', 'content': '![chart](report/charts/x.png)',
    }}]


@pytest.mark.asyncio
@pytest.mark.parametrize('late', [False, True])
async def test_capture_reads_the_project_folder_outside_the_run_context(monkeypatch, late):
    sandbox = PTCSandbox.__new__(PTCSandbox)
    sandbox._work_dir = '/home/workspace'
    sandbox.config = SimpleNamespace(filesystem=FilesystemConfig(working_directory=sandbox._work_dir))
    held = []

    async def download(path):
        held.append(f'download {path}')
        return b'project-image'

    sandbox.adownload_file_bytes = AsyncMock(side_effect=download)
    monkeypatch.setattr(_HOLD, _held(held))
    project = ProjectContext('ws-a', 'alpha-ab12')
    assert current_project() is None
    monkeypatch.setattr('src.server.services.persistence.image_capture.is_storage_enabled', lambda: True)
    monkeypatch.setattr('ptc_agent.agent.middleware.image_capture.upload_bytes', lambda *args: True)
    monkeypatch.setattr('ptc_agent.agent.middleware.image_capture.get_public_url', lambda key: 'https://images.example/'+key)
    placement = AsyncMock(return_value=SimpleNamespace(
        dir_name=project.dir_name, sibling_dir_names=(), layout_origin=None,
        previous_dir_names=(),
    ))
    monkeypatch.setattr('src.server.services.workspace_layout.resolve_project_placement', placement)
    events = [{'event': 'message_chunk', 'data': {
        'content_type': 'text', 'content': '![chart](report/charts/x.png)',
    }}]
    kwargs = {'workspace_id': project.workspace_id} if late else {'project': project}
    assert await capture_and_rewrite_images(events, sandbox, **kwargs) == 1
    sandbox.adownload_file_bytes.assert_awaited_once_with('/home/workspace/alpha-ab12/report/charts/x.png')
    assert 'https://images.example/' in events[0]['data']['content']
    if late:
        # Late, a settle may move the folder: it is read and downloaded from
        # under the hold, never from a name read before it.
        placement.assert_awaited_once_with('ws-a', root='/home/workspace')
        assert held == ['hold', 'download /home/workspace/alpha-ab12/report/charts/x.png', 'release']
    else:
        placement.assert_not_awaited()
        assert held == ['download /home/workspace/alpha-ab12/report/charts/x.png']
    assert current_project() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('moving', ['staged', 'held'])
async def test_late_capture_reads_nothing_from_a_folder_mid_move(monkeypatch, moving):
    sandbox = SimpleNamespace(working_dir='/home/workspace', adownload_file_bytes=AsyncMock())
    monkeypatch.setattr('src.server.services.persistence.image_capture.is_storage_enabled', lambda: True)
    monkeypatch.setattr(_HOLD, _held([], moving=moving == 'held'))
    monkeypatch.setattr('src.server.services.workspace_layout.resolve_project_placement', AsyncMock(
        return_value=SimpleNamespace(
            dir_name='_internal/moving/ws-a', sibling_dir_names=(), layout_origin=None,
            previous_dir_names=(),
        )))
    events = _chunk()
    assert await capture_and_rewrite_images(events, sandbox, workspace_id='ws-a') == 0
    sandbox.adownload_file_bytes.assert_not_awaited()
    assert events[0]['data']['content'] == '![chart](report/charts/x.png)'


@pytest.mark.asyncio
async def test_late_capture_never_falls_back_to_machine_root(monkeypatch):
    sandbox = SimpleNamespace(working_dir='/home/workspace', adownload_file_bytes=AsyncMock())
    monkeypatch.setattr('src.server.services.persistence.image_capture.is_storage_enabled', lambda: True)
    monkeypatch.setattr(_HOLD, _held([]))
    monkeypatch.setattr('src.server.services.workspace_layout.resolve_project_placement',
                        AsyncMock(side_effect=RuntimeError('missing binding')))
    events = [{'event': 'message_chunk', 'data': {
        'content_type': 'text', 'content': '![chart](report/charts/x.png)',
    }}]
    assert await capture_and_rewrite_images(events, sandbox, workspace_id='ws-a') == 0
    sandbox.adownload_file_bytes.assert_not_awaited()
    assert events[0]['data']['content'] == '![chart](report/charts/x.png)'
