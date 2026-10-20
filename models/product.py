from datetime import datetime, timezone

from models import db


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    sku = db.Column(db.String(64), unique=True, nullable=False)
    barcode = db.Column(db.String(64), unique=True, nullable=False)
    category = db.Column(db.String(80), nullable=False, default="General")
    size = db.Column(db.String(32))
    color = db.Column(db.String(48))
    image_filename = db.Column(db.String(80))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    cost = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    low_stock_threshold = db.Column(db.Integer, nullable=False, default=5)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    @property
    def is_low_stock(self):
        return self.quantity <= self.low_stock_threshold
