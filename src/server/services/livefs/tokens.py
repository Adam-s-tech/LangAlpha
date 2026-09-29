"""The token a computer's file mount authenticates with.

Opaque rather than signed: the server stores only its digest, so ending every
token a computer holds is one row delete, and a self-hosted server has no
signing secret to configure. The shape is ``lfs1.<computer_id>.<secret>``;
the computer id only says which row to compare against.

A token lives about an hour. The server writes a fresh one before the
current one runs low, at turn start and on the commands that run code, so a
copy taken out of the sandbox stops working within the hour, and at once
when the computer stops.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.server.database import livefs_tokens as db

PREFIX = "lfs1"
TOKEN_TTL = timedelta(hours=1)
# Rewrite once less than this is left. It has to outlast the longest command
# between two rewrites, since a background process keeps the token it saw.
REFRESH_BELOW = timedelta(minutes=30)


class LivefsAuthError(Exception):
    """The presented token is not a live one (never says why)."""


@dataclass(frozen=True)
class LivefsIdentity:
    computer_id: str
    user_id: str
    root_dir: str


@dataclass(frozen=True)
class MintedToken:
    token: str
    expires_at: datetime


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


async def mint_token(computer_id: str, user_id: str) -> MintedToken:
    secret = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + TOKEN_TTL
    await db.save_token(computer_id, user_id, _digest(secret), expires_at)
    return MintedToken(f"{PREFIX}.{computer_id}.{secret}", expires_at)


async def needs_refresh(computer_id: str) -> bool:
    expires_at = await db.token_expiry(computer_id)
    return expires_at is None or expires_at - datetime.now(UTC) < REFRESH_BELOW


async def revoke(computer_id: str) -> None:
    await db.delete_token(computer_id)


def _live(stored: bytes | None, expires_at: datetime | None, digest: bytes) -> bool:
    if stored is None or expires_at is None or expires_at <= datetime.now(UTC):
        return False
    return hmac.compare_digest(bytes(stored), digest)


async def authenticate(authorization: str | None) -> LivefsIdentity:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        raise LivefsAuthError
    prefix, _, rest = token.strip().partition(".")
    computer_id, _, secret = rest.partition(".")
    if prefix != PREFIX or not secret:
        raise LivefsAuthError
    try:
        computer_id = str(uuid.UUID(computer_id))
    except ValueError:
        raise LivefsAuthError from None
    row = await db.load_token(computer_id)
    if row is None:
        raise LivefsAuthError
    digest = _digest(secret)
    if not (
        _live(row["token_sha256"], row["expires_at"], digest)
        or _live(row["prev_token_sha256"], row["prev_expires_at"], digest)
    ):
        raise LivefsAuthError
    return LivefsIdentity(computer_id, row["user_id"], row["root_dir"])
