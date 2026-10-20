from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

from models.expense import Expense  # noqa: E402,F401
from models.product import Product  # noqa: E402,F401
from models.order import Order, OrderItem  # noqa: E402,F401
from models.sale import Sale, SaleItem  # noqa: E402,F401
from models.user import (  # noqa: E402,F401
    AuditLog,
    DEFAULT_PERMISSIONS,
    RevokedToken,
    User,
)
