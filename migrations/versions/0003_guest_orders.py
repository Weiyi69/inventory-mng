"""Add guest Cash on Delivery orders and link collected orders to sales."""

from alembic import op
import sqlalchemy as sa

revision = "0003_guest_orders"
down_revision = "0002_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "order",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_reference", sa.String(length=48), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("customer_name", sa.String(length=120), nullable=False),
        sa.Column("customer_email", sa.String(length=254), nullable=False),
        sa.Column("customer_phone", sa.String(length=32), nullable=False),
        sa.Column("delivery_address", sa.String(length=500), nullable=False),
        sa.Column("customer_note", sa.String(length=500)),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "payment_status",
            sa.String(length=16),
            nullable=False,
            server_default="unpaid",
        ),
        sa.Column("subtotal", sa.Numeric(10, 2), nullable=False),
        sa.Column("delivery_fee", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(10, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'shipped', 'delivered', 'cancelled')",
            name="ck_order_status",
        ),
        sa.CheckConstraint(
            "payment_status IN ('unpaid', 'collected')",
            name="ck_order_payment_status",
        ),
        sa.CheckConstraint(
            "subtotal >= 0 AND delivery_fee >= 0 AND total >= 0",
            name="ck_order_nonnegative_amounts",
        ),
        sa.UniqueConstraint("public_reference", name="uq_order_public_reference"),
        sa.UniqueConstraint("idempotency_key", name="uq_order_idempotency_key"),
    )
    op.create_index("ix_order_status", "order", ["status"])
    op.create_index("ix_order_created_at", "order", ["created_at"])

    op.create_table(
        "order_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("order.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("product.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("product_name", sa.String(length=120), nullable=False),
        sa.Column("product_sku", sa.String(length=64), nullable=False),
        sa.Column("product_category", sa.String(length=80)),
        sa.Column("product_size", sa.String(length=32)),
        sa.Column("product_color", sa.String(length=48)),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_cost", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        sa.CheckConstraint(
            "unit_price >= 0 AND unit_cost >= 0",
            name="ck_order_item_nonnegative_prices",
        ),
    )
    op.create_index("ix_order_item_order_id", "order_item", ["order_id"])
    op.create_index("ix_order_item_product_id", "order_item", ["product_id"])

    with op.batch_alter_table("sale") as batch_op:
        batch_op.add_column(sa.Column("order_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_sale_order_id_order",
            "order",
            ["order_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_sale_order_id", ["order_id"])


def downgrade():
    with op.batch_alter_table("sale") as batch_op:
        batch_op.drop_constraint("uq_sale_order_id", type_="unique")
        batch_op.drop_constraint("fk_sale_order_id_order", type_="foreignkey")
        batch_op.drop_column("order_id")

    op.drop_index("ix_order_item_product_id", table_name="order_item")
    op.drop_index("ix_order_item_order_id", table_name="order_item")
    op.drop_table("order_item")
    op.drop_index("ix_order_created_at", table_name="order")
    op.drop_index("ix_order_status", table_name="order")
    op.drop_table("order")
