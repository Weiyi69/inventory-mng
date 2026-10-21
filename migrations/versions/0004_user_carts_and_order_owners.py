"""Add persistent user carts and optional order owners."""

from alembic import op
import sqlalchemy as sa

revision = "0004_user_carts"
down_revision = "0003_guest_orders"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cart",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_cart_user_id"),
    )
    op.create_index("ix_cart_user_id", "cart", ["user_id"])
    op.create_table(
        "cart_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cart_id",
            sa.Integer(),
            sa.ForeignKey("cart.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("product.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quantity > 0",
            name="ck_cart_item_quantity_positive",
        ),
        sa.UniqueConstraint(
            "cart_id",
            "product_id",
            name="uq_cart_item_product",
        ),
    )
    op.create_index("ix_cart_item_cart_id", "cart_item", ["cart_id"])
    op.create_index("ix_cart_item_product_id", "cart_item", ["product_id"])

    with op.batch_alter_table("order") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_order_user_id_user",
            "user",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_order_user_id", ["user_id"])


def downgrade():
    with op.batch_alter_table("order") as batch_op:
        batch_op.drop_index("ix_order_user_id")
        batch_op.drop_constraint("fk_order_user_id_user", type_="foreignkey")
        batch_op.drop_column("user_id")

    op.drop_index("ix_cart_item_product_id", table_name="cart_item")
    op.drop_index("ix_cart_item_cart_id", table_name="cart_item")
    op.drop_table("cart_item")
    op.drop_index("ix_cart_user_id", table_name="cart")
    op.drop_table("cart")
