from datetime import datetime, timezone

from models import db


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    total = db.Column(db.Numeric(10, 2), nullable=False)
    amount_tendered = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    status = db.Column(
        db.String(16),
        nullable=False,
        default="completed",
        server_default="completed",
    )
    idempotency_key = db.Column(db.String(64), unique=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    order_id = db.Column(
        db.Integer,
        db.ForeignKey("order.id", ondelete="RESTRICT"),
        unique=True,
    )
    items = db.relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")
    created_by = db.relationship("User", back_populates="sales")
    order = db.relationship("Order", back_populates="sale")

    @property
    def change_due(self):
        return self.amount_tendered - self.total


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    product_size = db.Column(db.String(32))
    product_color = db.Column(db.String(48))
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    unit_cost = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    sale = db.relationship("Sale", back_populates="items")
    product = db.relationship("Product")
