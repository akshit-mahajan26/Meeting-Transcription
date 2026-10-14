"""add users and per-account ownership

Creates the users table, seeds the first account (test@test.com), adds an owning
user_id to projects, meetings, meeting_topics and speaker_turns, assigns every
existing row to the seeded account, then makes the column mandatory.

Revision ID: a3f1c9d27b40
Revises: e0d6e74c6561
Create Date: 2026-10-13 12:00:00.000000

"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from app.core.security import hash_password

# revision identifiers, used by Alembic.
revision: str = "a3f1c9d27b40"
down_revision: str | Sequence[str] | None = "e0d6e74c6561"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = ("projects", "meetings", "meeting_topics", "speaker_turns")
SEED_EMAIL = "test@test.com"
SEED_PASSWORD = "123456"


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

    # Reuse the seed account if it already exists, so existing signups are kept
    seed_id = bind.execute(sa.text("SELECT id FROM users WHERE email = :e"), {"e": SEED_EMAIL}).scalar()
    if seed_id is None:
        seed_id = str(uuid.uuid4())
        bind.execute(
            sa.text("INSERT INTO users (id, email, password_hash, created_at) VALUES (:i, :e, :p, :c)"),
            {
                "i": seed_id,
                "e": SEED_EMAIL,
                "p": hash_password(SEED_PASSWORD),
                "c": datetime.now(UTC).replace(tzinfo=None),
            },
        )

    for table in OWNED_TABLES:
        inspector = sa.inspect(bind)
        if "user_id" not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("user_id", sa.String(length=36), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL").bindparams(uid=seed_id))
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
