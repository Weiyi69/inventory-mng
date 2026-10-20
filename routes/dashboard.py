from functools import wraps
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, g, redirect, render_template, url_for
from sqlalchemy import func

from models import Expense, Product, Sale, SaleItem, db
from security import (
    access_required,
    discard_invalid_auth_cookies,
    optional_identity,
)

dashboard_bp = Blueprint("dashboard", __name__)


def _redirect_anonymous_to_catalog(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if optional_identity() is None:
            response = redirect(url_for("products.index"))
            return discard_invalid_auth_cookies(response)
        return view(*args, **kwargs)

    return wrapped


@dashboard_bp.get("/")
@_redirect_anonymous_to_catalog
@access_required("overview")
def index():
    show_financials = (
        g.current_user.role == "admin"
        or g.current_user.allows("dashboard.financial")
    )
    today = datetime.now(timezone.utc).date()
    report_days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    report_labels = []
    report_sales = []
    report_expenses = []
    report_profit = []
    for day in report_days:
        day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        report_labels.append(day.strftime("%b %d"))
        if show_financials:
            sales_total = db.session.query(
                func.coalesce(func.sum(Sale.total), 0)
            ).filter(
                Sale.created_at >= day_start,
                Sale.created_at < day_end,
                Sale.status == "completed",
            ).scalar()
            expense_total = db.session.query(
                func.coalesce(func.sum(Expense.amount), 0)
            ).filter(
                Expense.created_at >= day_start,
                Expense.created_at < day_end,
            ).scalar()
            report_sales.append(float(sales_total))
            report_expenses.append(float(expense_total))
            cost_of_goods = db.session.query(
                func.coalesce(func.sum(SaleItem.unit_cost * SaleItem.quantity), 0)
            ).join(Sale).filter(
                Sale.created_at >= day_start,
                Sale.created_at < day_end,
                Sale.status == "completed",
            ).scalar()
            report_profit.append(
                report_sales[-1] - float(cost_of_goods) - report_expenses[-1]
            )

    start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    today_sales = today_expenses = today_cost = inventory_value = Decimal("0")
    if show_financials:
        today_sales = db.session.query(
            func.coalesce(func.sum(Sale.total), 0)
        ).filter(
            Sale.created_at >= start,
            Sale.created_at < end,
            Sale.status == "completed",
        ).scalar()
        today_expenses = db.session.query(
            func.coalesce(func.sum(Expense.amount), 0)
        ).filter(
            Expense.created_at >= start, Expense.created_at < end
        ).scalar()
        today_cost = db.session.query(
            func.coalesce(func.sum(SaleItem.unit_cost * SaleItem.quantity), 0)
        ).join(Sale).filter(
            Sale.created_at >= start,
            Sale.created_at < end,
            Sale.status == "completed",
        ).scalar()
        inventory_value = db.session.query(
            func.coalesce(func.sum(Product.cost * Product.quantity), 0)
        ).scalar()
    sales_query = Sale.query.filter(Sale.status == "completed")
    if (
        g.current_user.role != "admin"
        and not g.current_user.allows("sales.view_all")
    ):
        sales_query = sales_query.filter(Sale.created_by_id == g.current_user.id)
    recent_sales = sales_query.order_by(Sale.created_at.desc()).limit(5).all()
    low_stock = Product.query.filter(
        Product.is_active.is_(True),
        Product.quantity <= Product.low_stock_threshold,
    ).order_by(Product.quantity.asc()).limit(8).all()
    low_stock_count = Product.query.filter(
        Product.is_active.is_(True),
        Product.quantity <= Product.low_stock_threshold,
    ).count()
    top_products = (
        db.session.query(
            SaleItem.product_name,
            func.sum(SaleItem.quantity).label("units"),
        )
        .join(Sale)
        .filter(Sale.status == "completed")
        .group_by(SaleItem.product_name)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(5)
        .all()
    )
    return render_template(
        "dashboard.html",
        today_sales=Decimal(today_sales),
        today_expenses=Decimal(today_expenses),
        today_profit=(
            Decimal(today_sales) - Decimal(today_cost) - Decimal(today_expenses)
        ),
        inventory_value=Decimal(inventory_value),
        product_count=Product.query.filter_by(is_active=True).count(),
        low_stock_count=low_stock_count,
        low_stock=low_stock,
        recent_sales=recent_sales,
        top_products=top_products,
        report_labels=report_labels if show_financials else [],
        report_sales=report_sales if show_financials else [],
        report_expenses=report_expenses if show_financials else [],
        report_profit=report_profit if show_financials else [],
        show_financials=show_financials,
    )
