"""Block unsafe rollback of mailbox OAuth storage.

Revision ID: 20260727_0039
Revises: 20260727_0038
Create Date: 2026-07-27 17:20:00

Revision 0038 introduced encrypted OAuth refresh credentials and OAuth-backed
mailbox channels.  Its schema downgrade must drop the credential tables and
the provider/authentication columns.  This revision deliberately performs no
schema upgrade; on a rollback it runs first and refuses to let 0038 discard
live OAuth state.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0039"
down_revision: Union[str, Sequence[str], None] = "20260727_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Install a rollback guard without changing the persisted schema."""


def _assert_0038_downgrade_is_safe() -> None:
    """Fail before revision 0038 can delete OAuth state.

    The two ``SELECT ... LIMIT 1`` checks intentionally use portable SQL that
    is valid on SQLite and PostgreSQL.  The guard executes while every table
    and column created by revision 0038 still exists, because Alembic invokes
    this revision's downgrade before proceeding to its parent.

    An unarchived channel is considered active even if temporarily disabled:
    it can be resumed, and downgrading would otherwise erase the fact that it
    requires OAuth rather than an app password.
    """

    bind = op.get_bind()
    refresh_credential_exists = bind.execute(
        sa.text("SELECT 1 FROM mailbox_oauth_credentials LIMIT 1")
    ).first() is not None
    active_oauth_mailbox_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM mailbox_configs "
            "WHERE authentication_mode = :oauth_mode "
            "AND archived_at IS NULL LIMIT 1"
        ),
        {"oauth_mode": "oauth2"},
    ).first() is not None

    if refresh_credential_exists or active_oauth_mailbox_exists:
        blocked_by: list[str] = []
        if refresh_credential_exists:
            blocked_by.append("encrypted OAuth refresh credentials")
        if active_oauth_mailbox_exists:
            blocked_by.append("active OAuth mailbox channels")
        raise RuntimeError(
            "mailbox_oauth_downgrade_blocked: revision 0038 would discard "
            + " and ".join(blocked_by)
            + "; archive/disconnect OAuth channels and remove their credentials "
            "before downgrading"
        )


def downgrade() -> None:
    """Guard the following 0038 downgrade against irreversible data loss."""

    _assert_0038_downgrade_is_safe()
