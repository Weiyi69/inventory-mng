"""Add users, permissions, audit history, and sale ownership."""

from alembic import op
import sqlalchemy as sa

revision = "0002_auth"
down_revision = "0001_legacy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "permissions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_user_email", "user", ["email"], unique=True)

    op.create_table(
        "revoked_token",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jti", sa.String(length=36), nullable=False),
        sa.Column("token_type", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("jti"),
    )
    op.create_index("ix_revoked_token_jti", "revoked_token", ["jti"], unique=True)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("target_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("ip_address", sa.String(length=45)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_log_event", "audit_log", ["event"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    with op.batch_alter_table("sale") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="completed")
        )
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64)))
        batch_op.add_column(sa.Column("created_by_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_sale_created_by_user",
            "user",
            ["created_by_id"],
            ["id"],
        )
        batch_op.create_unique_constraint("uq_sale_idempotency_key", ["idempotency_key"])

    with op.batch_alter_table("expense") as batch_op:
        batch_op.add_column(sa.Column("created_by_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_expense_created_by_user",
            "user",
            ["created_by_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("expense") as batch_op:
        batch_op.drop_constraint("fk_expense_created_by_user", type_="foreignkey")
        batch_op.drop_column("created_by_id")

    with op.batch_alter_table("sale") as batch_op:
        batch_op.drop_constraint("uq_sale_idempotency_key", type_="unique")
        batch_op.drop_constraint("fk_sale_created_by_user", type_="foreignkey")
        batch_op.drop_column("created_by_id")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("status")

    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_event", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_revoked_token_jti", table_name="revoked_token")
    op.drop_table("revoked_token")
    op.drop_index("ix_user_email", table_name="user")
    op.drop_table("user")
