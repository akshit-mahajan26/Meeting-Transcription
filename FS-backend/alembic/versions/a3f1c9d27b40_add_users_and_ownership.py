"""add users and per-account ownership

Creates the users table and adds an owning user_id to projects, meetings, meeting_topics
and speaker_turns. No account is created here: rows ingested before accounts existed keep
user_id NULL (invisible to everyone) and are adopted by the first account that signs up.
The column becomes NOT NULL only when no such rows exist.

Revision ID: a3f1c9d27b40
Revises: e0d6e74c6561
Create Date: 2026-10-13 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c9d27b40"
down_revision: str | Sequence[str] | None = "e0d6e74c6561"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = ("projects", "meetings", "meeting_topics", "speaker_turns")


def upgrade() -> None:
    """Idempotent: a `users` table or `user_id` columns may already exist if an earlier startup created them outside Alembic, so every step checks before it acts."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if "ix_users_email" not in {ix["name"] for ix in sa.inspect(bind).get_indexes("users")}:
        op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    for table in OWNED_TABLES:
        inspector = sa.inspect(bind)
        if "user_id" not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("user_id", sa.String(length=36), nullable=True))
        orphans = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")).scalar()
        if not orphans:
            op.alter_column(table, "user_id", nullable=False)
        if f"fk_{table}_user_id_users" not in {fk["name"] for fk in inspector.get_foreign_keys(table)}:
            op.create_foreign_key(f"fk_{table}_user_id_users", table, "users", ["user_id"], ["id"], ondelete="CASCADE")
        if f"ix_{table}_user_id" not in {ix["name"] for ix in inspector.get_indexes(table)}:
            op.create_index(op.f(f"ix_{table}_user_id"), table, ["user_id"], unique=False)


def downgrade() -> None:
    for table in OWNED_TABLES:
        op.drop_index(op.f(f"ix_{table}_user_id"), table_name=table)
        op.drop_constraint(f"fk_{table}_user_id_users", table, type_="foreignkey")
        op.drop_column(table, "user_id")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
