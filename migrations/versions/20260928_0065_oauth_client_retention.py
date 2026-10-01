"""Bound OAuth client retention after an authorization family is reclaimed.

Revision ID: 20260928_0065
Revises: 20260927_0064
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_0065"
down_revision: Union[str, Sequence[str], None] = "20260927_0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "integration_oauth_clients",
        sa.Column("last_authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE integration_oauth_clients "
        "SET last_authorized_at = ("
        "SELECT MAX(created_at) FROM integration_oauth_families "
        "WHERE integration_oauth_families.client_id = integration_oauth_clients.id) "
        "WHERE EXISTS (SELECT 1 FROM integration_oauth_families "
        "WHERE integration_oauth_families.client_id = integration_oauth_clients.id)"
    ))


def downgrade() -> None:
    op.drop_column("integration_oauth_clients", "last_authorized_at")
