from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text, true
from sqlalchemy.ext.mutable import MutableDict

from models import db

_password_hasher = PasswordHasher()

DEFAULT_PERMISSIONS = {
    "pages": {
        "overview": False,
        "products": True,
        "sales": False,
        "expenses": False,
        "reports": False,
    },
    "actions": {
        "products.write": False,
        "products.delete": False,
        "products.restock": False,
        "products.costs": False,
        "products.stock_view": False,
        "sales.process": False,
        "sales.view_all": False,
        "expenses.create": False,
        "expenses.view_all": False,
        "reports.financial": False,
        "dashboard.financial": False,
    },
}


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(
        db.String(16),
        nullable=False,
        default="user",
        server_default="user",
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    auth_version = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    permissions = db.Column(
        MutableDict.as_mutable(db.JSON),
        nullable=False,
        default=lambda: {
            key: value.copy() for key, value in DEFAULT_PERMISSIONS.items()
        },
        server_default=text("'{}'"),
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    sales = db.relationship("Sale", back_populates="created_by")
    expenses = db.relationship("Expense", back_populates="created_by")
    audit_events = db.relationship(
        "AuditLog",
        back_populates="actor",
        foreign_keys="AuditLog.actor_id",
    )

    def set_password(self, password):
        self.password_hash = _password_hasher.hash(password)

    def verify_password(self, password):
        try:
            valid = _password_hasher.verify(self.password_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            return False
        if valid and _password_hasher.check_needs_rehash(self.password_hash):
            self.set_password(password)
        return valid

    def allows(self, permission):
        if self.role == "admin":
            return True
        if permission != "products":
            if "." not in permission or not permission.startswith("products."):
                return False
        section, key = (
            ("actions", permission)
            if "." in permission
            else ("pages", permission)
        )
        return bool((self.permissions or {}).get(section, {}).get(key, False))


class RevokedToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(36), nullable=False, unique=True, index=True)
    token_type = db.Column(db.String(16), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    event = db.Column(db.String(80), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    details = db.Column(db.JSON, nullable=False, default=dict)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    actor = db.relationship(
        "User",
        back_populates="audit_events",
        foreign_keys=[actor_id],
    )
    target_user = db.relationship("User", foreign_keys=[target_user_id])
