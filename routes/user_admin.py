import copy
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from models import AuditLog, DEFAULT_PERMISSIONS, User, db
from security import access_required, audit_event

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PAGE_KEYS = ("overview", "products", "sales", "expenses", "reports")
ACTION_KEYS = (
    "products.write",
    "products.delete",
    "products.restock",
    "products.costs",
    "products.stock_view",
    "sales.process",
    "sales.view_all",
    "expenses.create",
    "expenses.view_all",
    "reports.financial",
    "dashboard.financial",
)
PERMISSION_LABELS = {
    "overview": "View overview",
    "products": "View products and inventory",
    "sales": "View sales",
    "expenses": "View expenses",
    "reports": "View reports",
    "products.write": "Create and change products/prices",
    "products.delete": "Delete or archive products",
    "products.restock": "Restock inventory",
    "products.costs": "View and change product costs",
    "products.stock_view": "View product stock quantities",
    "sales.process": "Process sales",
    "sales.view_all": "View all sales and invoices",
    "expenses.create": "Record expenses",
    "expenses.view_all": "View all expenses",
    "reports.financial": "View financial reports",
    "dashboard.financial": "View dashboard financial metrics",
}


def _checkboxes():
    permissions = copy.deepcopy(DEFAULT_PERMISSIONS)
    role = request.form.get("role", "user")
    if role == "admin":
        for page in PAGE_KEYS:
            permissions["pages"][page] = request.form.get(f"page_{page}") == "on"
    else:
        permissions["pages"]["products"] = True
    for action in ACTION_KEYS:
        section, key = action.split(".", 1)
        if role == "admin" or section == "products":
            permissions["actions"][action] = request.form.get(
                f"action_{section}_{key}"
            ) == "on"
    return permissions


def _valid_password(password):
    return isinstance(password, str) and len(password) >= 12


@admin_bp.get("/users")
@access_required(admin=True)
def users():
    return render_template(
        "admin_users.html",
        users=User.query.order_by(User.name, User.id).all(),
        page_keys=PAGE_KEYS,
        action_keys=ACTION_KEYS,
        labels=PERMISSION_LABELS,
        default_permissions=DEFAULT_PERMISSIONS,
    )


@admin_bp.post("/users")
@access_required(admin=True)
def create_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    if not name or len(name) > 120:
        flash("Enter a name of 1 to 120 characters.", "error")
        return redirect(url_for("admin.users"))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        flash("Enter a valid email address.", "error")
        return redirect(url_for("admin.users"))
    if not _valid_password(password):
        flash("Use a password with at least 12 characters.", "error")
        return redirect(url_for("admin.users"))
    if role not in {"admin", "user"}:
        abort(400, description="Choose a valid role.")
    if User.query.filter_by(email=email).first():
        flash("An account with that email already exists.", "error")
        return redirect(url_for("admin.users"))

    user = User(
        name=name,
        email=email,
        role=role,
        is_active=True,
        permissions=_checkboxes(),
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit_event(
        "user_created",
        actor=g.current_user,
        target=user,
        details={"email": email, "role": role},
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("An account with that email already exists.", "error")
        return redirect(url_for("admin.users"))
    flash(f"Account created for {name}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.post("/users/<int:user_id>")
@access_required(admin=True)
def update_user(user_id):
    user = User.query.get_or_404(user_id)
    current = g.current_user
    role = request.form.get("role", "")
    is_active = request.form.get("is_active") == "on"
    if role not in {"admin", "user"}:
        abort(400, description="Choose a valid role.")
    if user.id == current.id and (not is_active or role != "admin"):
        flash("You cannot deactivate or demote your own administrator account.", "error")
        return redirect(url_for("admin.users"))
    if user.role == "admin" and user.is_active and (
        not is_active or role != "admin"
    ):
        active_admins = User.query.filter_by(role="admin", is_active=True).count()
        if active_admins <= 1:
            flash("At least one active administrator must remain.", "error")
            return redirect(url_for("admin.users"))
    old_role, old_active = user.role, user.is_active
    user.role = role
    user.is_active = is_active
    user.auth_version += 1
    user.permissions = _checkboxes()
    audit_event(
        "user_updated",
        actor=current,
        target=user,
        details={
            "old_role": old_role,
            "new_role": role,
            "old_active": old_active,
            "new_active": is_active,
            "permissions": user.permissions,
        },
    )
    db.session.commit()
    flash(f"Updated account for {user.name}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.get("/audit")
@access_required(admin=True)
def audit():
    events = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(250).all()
    return render_template("admin_audit.html", events=events)
