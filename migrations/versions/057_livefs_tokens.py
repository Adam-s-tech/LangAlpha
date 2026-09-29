"""Migration 057: the credential a computer's file mount presents.

A running computer mounts the user's memory, profile, workflows and
transcripts as files, served by the server on each read and write. The mount
authenticates with a random token written into the sandbox; only its SHA-256
is kept here. The previous token stays valid until its own expiry so a
rewrite never races a request already in flight. Stopping, rebuilding or
deleting the computer deletes the row, which ends every token it held.

``held_by`` names the sandbox that took the current token, since a mint can
land without its publish; a worker adopts a token without an exec only when
its own sandbox took it.
"""

from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS livefs_tokens (
            computer_id UUID PRIMARY KEY
                REFERENCES computers(computer_id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            token_sha256 BYTEA NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            prev_token_sha256 BYTEA,
            prev_expires_at TIMESTAMPTZ,
            held_by VARCHAR(255),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS livefs_tokens")
