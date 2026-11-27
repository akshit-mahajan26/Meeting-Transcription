"""meetings.excluded_at: a synced meeting the user deleted (no new tables)

A deleted meeting that came from a Fireflies sync keeps its row as a hidden marker (status 'EXCLUDED', no topics or
turns) so later syncs do not add it again. excluded_at records when it was deleted. Pasting its link re-adds it.

Revision ID: a8c5e2d91f36
Revises: f2b8d6a40c71
Create Date: 2026-11-28 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8c5e2d91f36"
down_revision: str | Sequence[str] | None = "f2b8d6a40c71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Idempotent."""
    if "excluded_at" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("meetings")}:
        op.add_column("meetings", sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if "excluded_at" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("meetings")}:
        op.drop_column("meetings", "excluded_at")
