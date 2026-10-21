import secrets
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint

from models import db


class Order(db.Model):
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'shipped', 'delivered', 'cancelled')",
            name="ck_order_status",
        ),
        CheckConstraint(
            "payment_status IN ('unpaid', 'collected')",
            name="ck_order_payment_status",
        ),
        CheckConstraint(
            "subtotal >= 0 AND delivery_fee >= 0 AND total >= 0",
            name="ck_order_nonnegative_amounts",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    public_reference = db.Column(db.String(48), nullable=False, unique=True)
    idempotency_key = db.Column(db.String(64), nullable=False, unique=True)
    request_digest = db.Column(db.String(64), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_name = db.Column(db.String(120), nullable=False)
    customer_email = db.Column(db.String(254), nullable=False)
    customer_phone = db.Column(db.String(32), nullable=False)
    delivery_address = db.Column(db.String(500), nullable=False)
    customer_note = db.Column(db.String(500))
    status = db.Column(
        db.String(24),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    payment_status = db.Column(
        db.String(16),
        nullable=False,
        default="unpaid",
        server_default="unpaid",
    )
    subtotal = db.Column(db.Numeric(10, 2), nullable=False)
    delivery_fee = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    total = db.Column(db.Numeric(10, 2), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    items = db.relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.id",
    )
    user = db.relationship("User")
    sale = db.relationship("Sale", back_populates="order", uselist=False)

    @staticmethod
    def new_public_reference():
        return secrets.token_urlsafe(24)


class OrderItem(db.Model):
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint(
            "unit_price >= 0 AND unit_cost >= 0",
            name="ck_order_item_nonnegative_prices",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer,
        db.ForeignKey("order.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product_name = db.Column(db.String(120), nullable=False)
    product_sku = db.Column(db.String(64), nullable=False)
    product_category = db.Column(db.String(80))
    product_size = db.Column(db.String(32))
    product_color = db.Column(db.String(48))
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    unit_cost = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product")
