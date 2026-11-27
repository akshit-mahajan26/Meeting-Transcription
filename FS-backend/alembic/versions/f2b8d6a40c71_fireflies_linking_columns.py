"""fireflies account linking columns (no new tables)

users: the Fireflies connection (encrypted key, account email, settings, sync state).
meetings: `source` (link or fireflies_sync), `purge_at` (7-day retention), `retry_until` (failed-meeting retries).
Existing meetings become source 'link'. Existing FAILED meetings get retry_until = now(), so they show as
"retries stopped" with a Retry button rather than as skipped (plan section 5.2).

Revision ID: f2b8d6a40c71
Revises: d7a9c3e15b42
Create Date: 2026-11-28 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2b8d6a40c71"
down_revision: str | Sequence[str] | None = "d7a9c3e15b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)

USER_COLUMNS = (
    sa.Column("fireflies_api_key_encrypted", sa.Text(), nullable=True),
    sa.Column("fireflies_account_email", sa.String(255), nullable=True),
    sa.Column("fireflies_auto_sync", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("fireflies_sync_project_id", sa.String(100), nullable=True),
    sa.Column("fireflies_sync_heartbeat_at", TZ, nullable=True),
    sa.Column("fireflies_last_synced_at", TZ, nullable=True),
    sa.Column("fireflies_last_full_sync_at", TZ, nullable=True),
    sa.Column("fireflies_last_sync_result", postgresql.JSONB(), nullable=True),
)
MEETING_COLUMNS = (
    sa.Column("source", sa.String(20), nullable=False, server_default="link"),
    sa.Column("purge_at", TZ, nullable=True),
    sa.Column("retry_until", TZ, nullable=True),
)
SYNC_PROJECT_FK = "fk_users_fireflies_sync_project_id_projects"
PURGE_INDEX = "ix_meetings_purge_at"


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Idempotent: each step checks before it acts."""
    bind = op.get_bind()
    for table, columns in (("users", USER_COLUMNS), ("meetings", MEETING_COLUMNS)):
        existing = _columns(bind, table)
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column.copy())
                if table == "meetings" and column.name == "retry_until":
                    op.execute("UPDATE meetings SET retry_until = now() WHERE status = 'FAILED' AND retry_until IS NULL")
    inspector = sa.inspect(bind)
    if SYNC_PROJECT_FK not in {fk["name"] for fk in inspector.get_foreign_keys("users")}:
        op.create_foreign_key(
            SYNC_PROJECT_FK, "users", "projects", ["fireflies_sync_project_id"], ["id"], ondelete="SET NULL"
        )
    if PURGE_INDEX not in {ix["name"] for ix in inspector.get_indexes("meetings")}:
        op.create_index(PURGE_INDEX, "meetings", ["purge_at"], postgresql_where=sa.text("purge_at IS NOT NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if PURGE_INDEX in {ix["name"] for ix in inspector.get_indexes("meetings")}:
        op.drop_index(PURGE_INDEX, table_name="meetings")
    if SYNC_PROJECT_FK in {fk["name"] for fk in inspector.get_foreign_keys("users")}:
        op.drop_constraint(SYNC_PROJECT_FK, "users", type_="foreignkey")
    for table, columns in (("meetings", MEETING_COLUMNS), ("users", USER_COLUMNS)):
        existing = _columns(bind, table)
        for column in columns:
            if column.name in existing:
                op.drop_column(table, column.name)
