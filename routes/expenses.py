from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from models import Expense, db
from security import access_required

expenses_bp = Blueprint("expenses", __name__, url_prefix="/expenses")


@expenses_bp.get("/")
@access_required("expenses")
def index():
    query = Expense.query
    if (
        g.current_user.role != "admin"
        and not g.current_user.allows("expenses.view_all")
    ):
        query = query.filter(Expense.created_by_id == g.current_user.id)
    expenses = query.order_by(Expense.created_at.desc()).limit(100).all()
    return render_template(
        "expenses.html",
        expenses=expenses,
        can_create=g.current_user.allows("expenses.create"),
    )


@expenses_bp.post("/")
@access_required("expenses", "expenses.create")
def create():
    try:
        description = request.form.get("description", "").strip()
        amount = Decimal(request.form.get("amount", ""))
        if not description:
            raise ValueError("Expense description is required.")
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Expense amount must be greater than zero.")
        expense = Expense(
            description=description,
            category=request.form.get("category", "").strip() or "General",
            amount=amount.quantize(Decimal("0.01")),
            created_by_id=g.current_user.id,
        )
        db.session.add(expense)
        db.session.commit()
    except (ValueError, InvalidOperation) as error:
        db.session.rollback()
        flash(str(error) or "Enter a valid expense amount.", "error")
    else:
        flash("Expense recorded.", "success")
    return redirect(url_for("expenses.index"))
