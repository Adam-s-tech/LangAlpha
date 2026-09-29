"""The mount token a computer's file daemon authenticates with.

The server keeps only a digest per slot (current and previous), so a copy
taken out of a sandbox stops working at its expiry, after two rotations, or
at once on revocation. A daemon mid-request keeps working across one
rotation, which is why the previous slot exists at all.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from src.server.services.livefs import tokens
from src.server.services.livefs.tokens import (
    LivefsAuthError,
    LivefsIdentity,
    authenticate,
    mint_token,
    needs_refresh,
    revoke,
)

COMPUTER = "c0ffee00-0000-4000-8000-000000000001"
OTHER_COMPUTER = "c0ffee00-0000-4000-8000-000000000002"
USER = "user-livefs-test"
ROOT = "/home/workspace"


class _TokenTable:
    """``livefs_tokens`` joined to its computer, in memory.

    ``save_token`` mirrors the upsert: the current digest and expiry move to
    the previous slot, and whatever sat there is gone.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.lookups: list[str] = []

    async def save_token(self, computer_id, user_id, digest, expires_at) -> None:
        row = self.rows.get(computer_id)
        if row is None:
            self.rows[computer_id] = {
                "user_id": user_id,
                "token_sha256": digest,
                "expires_at": expires_at,
                "prev_token_sha256": None,
                "prev_expires_at": None,
            }
            return
        row.update(
            prev_token_sha256=row["token_sha256"],
            prev_expires_at=row["expires_at"],
            token_sha256=digest,
            expires_at=expires_at,
            user_id=user_id,
        )

    async def load_token(self, computer_id):
        self.lookups.append(computer_id)
        row = self.rows.get(computer_id)
        return None if row is None else {**row, "root_dir": ROOT}

    async def token_expiry(self, computer_id):
        row = self.rows.get(computer_id)
        return None if row is None else row["expires_at"]

    async def delete_token(self, computer_id) -> None:
        self.rows.pop(computer_id, None)


@pytest.fixture
def table(monkeypatch) -> _TokenTable:
    fake = _TokenTable()
    monkeypatch.setattr(tokens, "db", fake)
    return fake


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def _secret(token: str) -> str:
    return token.split(".", 2)[2]


async def _rejects(authorization: str | None) -> bool:
    try:
        await authenticate(authorization)
    except LivefsAuthError:
        return True
    return False


# -- minting -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_minted_token_names_its_computer_and_the_server_keeps_only_its_digest(
    table,
):
    minted = await mint_token(COMPUTER, USER)

    prefix, computer_id, secret = minted.token.split(".", 2)
    assert (prefix, computer_id) == ("lfs1", COMPUTER)
    row = table.rows[COMPUTER]
    assert row["token_sha256"] == hashlib.sha256(secret.encode()).digest()
    assert row["expires_at"] == minted.expires_at
    lifetime = minted.expires_at - datetime.now(UTC)
    assert timedelta(minutes=59) < lifetime <= timedelta(hours=1)


# -- authentication --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_minted_token_authenticates_as_its_computer_user_and_root(table):
    minted = await mint_token(COMPUTER, USER)

    identity = await authenticate(_bearer(minted.token))

    assert identity == LivefsIdentity(COMPUTER, USER, ROOT)


@pytest.mark.asyncio
async def test_a_forged_secret_for_a_real_computer_is_rejected(table):
    await mint_token(COMPUTER, USER)

    assert await _rejects(_bearer(f"lfs1.{COMPUTER}.not-the-minted-secret"))


@pytest.mark.asyncio
async def test_a_secret_presented_under_another_computers_id_is_rejected(table):
    """The id picks the row to compare against, so a token moved onto a
    second computer of the same user must not match that computer's row."""
    minted = await mint_token(COMPUTER, USER)
    await mint_token(OTHER_COMPUTER, USER)

    moved = f"lfs1.{OTHER_COMPUTER}.{_secret(minted.token)}"

    assert await _rejects(_bearer(moved))


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        f"Basic lfs1.{COMPUTER}.secret",
        "Bearer",
        f"Bearer lfs2.{COMPUTER}.secret",
        f"Bearer lfs1.{COMPUTER}",
        f"Bearer lfs1.{COMPUTER}.",
        "Bearer lfs1.not-a-uuid.secret",
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_authorization_is_rejected_before_any_lookup(
    table, authorization
):
    """A computer id that is not a UUID would fail the uuid column as a
    database error; it has to answer as a bad token instead."""
    assert await _rejects(authorization)
    assert table.lookups == []


@pytest.mark.asyncio
async def test_an_expired_token_is_rejected(table):
    minted = await mint_token(COMPUTER, USER)
    table.rows[COMPUTER]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    assert await _rejects(_bearer(minted.token))


# -- rotation and revocation -----------------------------------------------------


@pytest.mark.asyncio
async def test_after_a_rotation_the_previous_token_still_authenticates(table):
    """A daemon mid-request holds the token it read before the rewrite."""
    first = await mint_token(COMPUTER, USER)
    second = await mint_token(COMPUTER, USER)

    assert (await authenticate(_bearer(first.token))).computer_id == COMPUTER
    assert (await authenticate(_bearer(second.token))).computer_id == COMPUTER


@pytest.mark.asyncio
async def test_a_second_rotation_drops_the_token_from_two_mints_ago(table):
    first = await mint_token(COMPUTER, USER)
    await mint_token(COMPUTER, USER)
    await mint_token(COMPUTER, USER)

    assert await _rejects(_bearer(first.token))


@pytest.mark.asyncio
async def test_the_previous_token_stops_at_its_own_expiry(table):
    first = await mint_token(COMPUTER, USER)
    await mint_token(COMPUTER, USER)
    table.rows[COMPUTER]["prev_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    assert await _rejects(_bearer(first.token))


@pytest.mark.asyncio
async def test_revocation_ends_the_current_and_the_previous_token(table):
    first = await mint_token(COMPUTER, USER)
    second = await mint_token(COMPUTER, USER)

    await revoke(COMPUTER)

    assert await _rejects(_bearer(first.token))
    assert await _rejects(_bearer(second.token))


# -- refresh -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes_left", "due"),
    [(31, False), (29, True), (-5, True)],
)
@pytest.mark.asyncio
async def test_a_token_is_due_for_refresh_once_under_thirty_minutes_remain(
    table, minutes_left, due
):
    await mint_token(COMPUTER, USER)
    table.rows[COMPUTER]["expires_at"] = datetime.now(UTC) + timedelta(
        minutes=minutes_left
    )

    assert await needs_refresh(COMPUTER) is due


@pytest.mark.asyncio
async def test_a_computer_holding_no_token_is_due_for_refresh(table):
    assert await needs_refresh(COMPUTER) is True
