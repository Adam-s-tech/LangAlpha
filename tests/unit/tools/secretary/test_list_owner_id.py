"""The workspace listing answers without the owner's user_id.

The tool reads the user from the run config, so the id in each row is never
something the model passes back; the workspace id it does pass back stays.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.tools.secretary import tools

pytestmark = pytest.mark.asyncio

OWNER = "owner-5c81e2"


async def test_workspace_list_drops_the_owner_user_id():
    row = {
        "workspace_id": "ws-1",
        "user_id": OWNER,
        "name": "Semis",
        "sandbox_id": "sbx-1",
        "status": "running",
        "created_at": datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
    }
    with patch(
        "src.server.database.workspace.get_workspaces_for_user",
        AsyncMock(return_value=([row], 1)),
    ):
        command = await tools._workspaces_list(OWNER, "call-1")
    content = command.update["messages"][0].content
    assert OWNER not in content
    listed = json.loads(content)["workspaces"]
    assert listed == [
        {
            "workspace_id": "ws-1",
            "name": "Semis",
            "sandbox_id": "sbx-1",
            "status": "running",
            "created_at": "2026-09-30 12:00:00+00:00",
        }
    ]
