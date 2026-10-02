"""The user data tools answer without the owner's user_id.

The tools read the user from the run config, so the id in each row is never
something the model passes back, and a tool answer is stored with the thread
that a public share replays.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tools.user_profile import tools

pytestmark = pytest.mark.asyncio

OWNER = "owner-5c81e2"
CREATED = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

_WATCHLIST = {
    "watchlist_id": "wl-1",
    "user_id": OWNER,
    "name": "Semis",
    "description": None,
    "is_default": True,
    "display_order": 0,
    "created_at": CREATED,
    "updated_at": CREATED,
}
_ITEM = {
    "watchlist_item_id": "wi-1",
    "watchlist_id": "wl-1",
    "user_id": OWNER,
    "symbol": "NVDA",
    "instrument_type": "stock",
    "exchange": None,
    "name": None,
    "notes": None,
    "alert_settings": {},
    "metadata": {},
    "created_at": CREATED,
    "updated_at": CREATED,
}
_HOLDING = {
    "user_portfolio_id": "up-1",
    "user_id": OWNER,
    "symbol": "AAPL",
    "instrument_type": "stock",
    "exchange": None,
    "name": None,
    "quantity": Decimal("10"),
    "average_cost": Decimal("150"),
    "currency": "USD",
    "account_name": None,
    "notes": None,
    "metadata": {},
    "first_purchased_at": None,
    "created_at": CREATED,
    "updated_at": CREATED,
}
_MERGE = {
    "previous": {"quantity": "5", "average_cost": "140"},
    "added": {"quantity": "5", "average_cost": "160"},
    "result": {"quantity": "10", "average_cost": "150"},
}


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr(
        tools,
        "user_db",
        SimpleNamespace(
            get_user=AsyncMock(return_value={"user_id": OWNER, "name": "A", "timezone": "UTC", "locale": "en"}),
            get_user_preferences=AsyncMock(
                return_value={"user_id": OWNER, "risk_preference": {"level": "moderate"}}
            ),
        ),
    )
    monkeypatch.setattr(
        tools,
        "watchlist_db",
        SimpleNamespace(
            get_user_watchlists=AsyncMock(return_value=[dict(_WATCHLIST)]),
            get_watchlist_items=AsyncMock(return_value=[dict(_ITEM)]),
            get_or_create_default_watchlist=AsyncMock(return_value=dict(_WATCHLIST)),
            create_watchlist=AsyncMock(return_value=dict(_WATCHLIST)),
            create_watchlist_item=AsyncMock(return_value=dict(_ITEM)),
        ),
    )
    monkeypatch.setattr(
        tools,
        "portfolio_db",
        SimpleNamespace(
            get_user_portfolio=AsyncMock(return_value=[dict(_HOLDING)]),
            upsert_portfolio_holding=AsyncMock(return_value=(dict(_HOLDING), dict(_MERGE))),
        ),
    )
    monkeypatch.setattr(tools, "maybe_complete_onboarding", AsyncMock())


async def _stored_answer(tool, args: dict) -> str:
    """The answer as the model reads it and the thread stores it."""
    message = await tool.ainvoke(
        {"name": tool.name, "args": args, "id": "call-1", "type": "tool_call"},
        config={"configurable": {"user_id": OWNER}},
    )
    return message.content


@pytest.mark.parametrize(
    ("tool", "args", "row_id"),
    [
        (tools.get_user_data, {"entity": "all"}, "wl-1"),
        (tools.get_user_data, {"entity": "watchlists"}, "wl-1"),
        (tools.get_user_data, {"entity": "watchlist_items"}, "wi-1"),
        (tools.get_user_data, {"entity": "portfolio"}, "up-1"),
        (tools.update_user_data, {"entity": "watchlist", "data": {"name": "Semis"}}, "wl-1"),
        (tools.update_user_data, {"entity": "watchlist_item", "data": {"symbol": "NVDA"}}, "wi-1"),
        (
            tools.update_user_data,
            {"entity": "portfolio_holding", "data": {"symbol": "AAPL", "quantity": 5}},
            "up-1",
        ),
    ],
)
async def test_answer_carries_no_owner_id(tool, args, row_id):
    content = await _stored_answer(tool, args)

    assert OWNER not in content
    assert "user_id" not in content
    # The row ids stay: the model passes watchlist_id back to scope a call.
    assert row_id in content


async def test_all_keeps_every_section_and_the_merge():
    data = await tools.get_user_data.ainvoke({"entity": "all"}, config={"configurable": {"user_id": OWNER}})
    assert data["profile"] == {"name": "A", "timezone": "UTC", "locale": "en"}
    assert data["watchlists"][0]["items"][0]["symbol"] == "NVDA"
    assert data["portfolio"][0]["quantity"] == Decimal("10")

    merged = await tools.update_user_data.ainvoke(
        {"entity": "portfolio_holding", "data": {"symbol": "AAPL", "quantity": 5}},
        config={"configurable": {"user_id": OWNER}},
    )
    assert merged["merged"] is True
    assert merged["merge_details"] == _MERGE
