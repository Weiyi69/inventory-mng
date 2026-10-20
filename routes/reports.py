from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, abort, g, render_template, request
from sqlalchemy import func

from models import Expense, Sale, SaleItem, db
from security import access_required

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

REPORT_RANGES = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "4w": "Last 4 weeks",
    "all": "All time",
}


def _month_start(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _report_buckets(range_key, now):
    if range_key == "24h":
        start = now - timedelta(hours=24)
        return [
            (
                start + timedelta(hours=index),
                timedelta(hours=1),
                (start + timedelta(hours=index)).strftime("%I %p").lstrip("0"),
            )
            for index in range(24)
        ]
    if range_key == "7d":
        today = now.date()
        return [
            (
                datetime.combine(
                    today - timedelta(days=offset),
                    time.min,
                    tzinfo=timezone.utc,
                ),
                timedelta(days=1),
                (today - timedelta(days=offset)).strftime("%b %d"),
            )
            for offset in range(6, -1, -1)
        ]
    if range_key == "4w":
        end = now
        start = end - timedelta(days=28)
        return [
            (start + timedelta(days=7 * index), timedelta(days=7), f"Week {index + 1}")
            for index in range(4)
        ]

    first_event = db.session.query(
        func.min(Sale.created_at).label("first_sale"),
    ).filter(Sale.status == "completed").one()
    first_sale = first_event.first_sale
    first_expense = db.session.query(func.min(Expense.created_at)).scalar()
    event_dates = [value for value in (first_sale, first_expense) if value is not None]
    first_month = min(event_dates).replace(tzinfo=timezone.utc) if event_dates else now
    current_month = _month_start(now)
    buckets = []
    month = _month_start(first_month)
    while month <= current_month:
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        buckets.append((month, next_month - month, month.strftime("%b %Y")))
        month = next_month
    return buckets


@reports_bp.get("/")
@access_required("reports")
def index():
    range_key = request.args.get("range", "7d")
    if range_key not in REPORT_RANGES:
        abort(400, description="Choose a supported report range.")

    now = datetime.now(timezone.utc)
    buckets = _report_buckets(range_key, now)
    labels, sales_data, expense_data, profit_data = [], [], [], []
    show_financials = (
        g.current_user.role == "admin"
        or g.current_user.allows("reports.financial")
    )
    if show_financials:
        for start, duration, label in buckets:
            end = start + duration
            sales_total = db.session.query(
                func.coalesce(func.sum(Sale.total), 0)
            ).filter(
                Sale.created_at >= start,
                Sale.created_at < end,
                Sale.status == "completed",
            ).scalar()
            expense_total = db.session.query(
                func.coalesce(func.sum(Expense.amount), 0)
            ).filter(
                Expense.created_at >= start, Expense.created_at < end
            ).scalar()
            cost_total = db.session.query(
                func.coalesce(func.sum(SaleItem.unit_cost * SaleItem.quantity), 0)
            ).join(Sale).filter(
                Sale.created_at >= start,
                Sale.created_at < end,
                Sale.status == "completed",
            ).scalar()
            labels.append(label)
            sales_data.append(float(sales_total))
            expense_data.append(float(expense_total))
            profit_data.append(float(sales_total - cost_total - expense_total))

        products = (
            db.session.query(
                SaleItem.product_name,
                func.sum(SaleItem.quantity).label("units"),
                func.sum(SaleItem.unit_price * SaleItem.quantity).label("revenue"),
            )
            .join(Sale)
            .filter(Sale.status == "completed")
            .group_by(SaleItem.product_name)
            .order_by(func.sum(SaleItem.unit_price * SaleItem.quantity).desc())
            .limit(10)
            .all()
        )
    else:
        products = (
            db.session.query(
                SaleItem.product_name,
                func.sum(SaleItem.quantity).label("units"),
            )
            .join(Sale)
            .filter(
                Sale.status == "completed",
                Sale.created_by_id == g.current_user.id,
            )
            .group_by(SaleItem.product_name)
            .order_by(func.sum(SaleItem.quantity).desc())
            .limit(10)
            .all()
        )
    return render_template(
        "reports.html",
        labels=labels,
        sales_data=sales_data,
        expense_data=expense_data,
        profit_data=profit_data,
        products=products,
        selected_range=range_key,
        range_label=REPORT_RANGES[range_key],
        range_description={
            "24h": "Hourly totals for the last 24 hours.",
            "7d": "Daily totals for the last seven days.",
            "4w": "Weekly totals for the last four weeks.",
            "all": "Monthly totals for all recorded activity.",
        }[range_key],
        show_financials=show_financials,
    )
