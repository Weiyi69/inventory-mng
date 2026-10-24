import json
import os
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import (
    abort,
    Blueprint,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import joinedload, selectinload

from models import Product, Sale, SaleItem, db
from security import access_required
from sqlalchemy.exc import IntegrityError

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")


def _peso_pdf_fonts():
    windows_fonts = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"),
        "Fonts",
    )
    candidates = [
        (
            os.path.join(windows_fonts, "arial.ttf"),
            os.path.join(windows_fonts, "arialbd.ttf"),
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ),
    ]
    for regular_path, bold_path in candidates:
        if not os.path.isfile(regular_path) or not os.path.isfile(bold_path):
            continue
        regular_font = TTFont("RetailFlowPeso", regular_path)
        bold_font = TTFont("RetailFlowPesoBold", bold_path)
        peso_codepoint = ord("₱")
        if (
            peso_codepoint not in regular_font.face.charWidths
            or peso_codepoint not in bold_font.face.charWidths
        ):
            continue
        registered_fonts = pdfmetrics.getRegisteredFontNames()
        if "RetailFlowPeso" not in registered_fonts:
            pdfmetrics.registerFont(regular_font)
        if "RetailFlowPesoBold" not in registered_fonts:
            pdfmetrics.registerFont(bold_font)
        return "RetailFlowPeso", "RetailFlowPesoBold"
    raise RuntimeError(
        "A TrueType font with Philippine peso support is required for PDF invoices."
    )


def _parse_items(data):
    if isinstance(data, dict):
        items = data.get("items")
    else:
        items = data
    if not isinstance(items, list) or not items:
        raise ValueError("Add at least one product to the sale.")
    quantities = {}
    for item in items:
        try:
            product_id = int(item["product_id"])
            quantity = int(item["quantity"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each sale item needs a valid product and quantity.")
        if product_id < 1 or quantity < 1:
            raise ValueError("Product and quantity must be greater than zero.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
    return quantities


@sales_bp.get("/")
@access_required("sales")
def index():
    sales_query = Sale.query.options(
        selectinload(Sale.items).joinedload(SaleItem.product)
    ).filter(Sale.status == "completed")
    if (
        g.current_user.role != "admin"
        and not g.current_user.allows("sales.view_all")
    ):
        sales_query = sales_query.filter(Sale.created_by_id == g.current_user.id)
    sales = sales_query.order_by(Sale.created_at.desc()).limit(100).all()
    products = Product.query.filter(
        Product.is_active.is_(True), Product.quantity > 0
    ).order_by(Product.name).all()
    return render_template(
        "sales.html",
        sales=sales,
        products=products,
        can_process=g.current_user.allows("sales.process"),
    )


def _duplicate_sale(sale):
    if request.is_json:
        return {
            "id": sale.id,
            "total": str(sale.total),
            "change_due": str(sale.change_due),
            "invoice_url": url_for("sales.invoice", sale_id=sale.id),
            "duplicate": True,
        }, 200
    flash(f"Sale #{sale.id} was already completed.", "success")
    return redirect(url_for("sales.invoice", sale_id=sale.id))


@sales_bp.post("/")
@access_required("sales", "sales.process")
def create():
    idempotency_key = (
        request.headers.get("Idempotency-Key", "")
        if request.is_json
        else request.form.get("idempotency_key", "")
    ).strip()
    if not idempotency_key or len(idempotency_key) > 64:
        message = "A valid sale submission key is required. Please retry the sale."
        if request.is_json:
            return {"error": message}, 400
        flash(message, "error")
        return redirect(url_for("sales.index"))

    existing = Sale.query.filter_by(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.created_by_id != g.current_user.id
            and g.current_user.role != "admin"
        ):
            return {"error": "This sale submission key is unavailable."}, 409
        return _duplicate_sale(existing)

    try:
        data = request.get_json(silent=True) if request.is_json else None
        if data is None:
            raw_items = request.form.get("items", "")
            data = json.loads(raw_items) if raw_items else {
                "items": [{"product_id": request.form.get("product_id"),
                           "quantity": request.form.get("quantity")}]
            }
        quantities = _parse_items(data)
        products = Product.query.filter(
            Product.id.in_(quantities), Product.is_active.is_(True)
        ).all()
        if len(products) != len(quantities):
            raise ValueError("One or more selected products do not exist.")
        for product in products:
            if product.quantity < quantities[product.id]:
                raise ValueError(f"Not enough stock for {product.name}.")

        sale = Sale(
            total=Decimal("0.00"),
            created_by_id=g.current_user.id,
            idempotency_key=idempotency_key,
        )
        total = Decimal("0.00")
        for product in products:
            quantity = quantities[product.id]
            stock_update = Product.query.filter(
                Product.id == product.id,
                Product.is_active.is_(True),
                Product.quantity >= quantity,
            ).update(
                {Product.quantity: Product.quantity - quantity},
                synchronize_session=False,
            )
            if stock_update != 1:
                raise ValueError(f"Not enough stock for {product.name}.")
            total += product.price * quantity
            sale.items.append(SaleItem(
                product=product,
                product_name=product.name,
                product_size=product.size,
                product_color=product.color,
                quantity=quantity,
                unit_price=product.price,
                unit_cost=product.cost,
            ))
        sale.total = total.quantize(Decimal("0.01"))
        tendered_value = data.get("amount_tendered") if isinstance(data, dict) else None
        if tendered_value in (None, ""):
            tendered = sale.total
        else:
            tendered = Decimal(str(tendered_value))
            if not tendered.is_finite() or tendered < sale.total:
                raise ValueError("Amount tendered must be at least the sale total.")
        sale.amount_tendered = tendered.quantize(Decimal("0.01"))
        db.session.add(sale)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Sale.query.filter_by(idempotency_key=idempotency_key).first()
        if existing and (
            existing.created_by_id == g.current_user.id
            or g.current_user.role == "admin"
        ):
            return _duplicate_sale(existing)
        raise
    except (ValueError, InvalidOperation) as error:
        db.session.rollback()
        message = str(error) or "Sale data is invalid."
        if request.is_json:
            return {"error": message}, 400
        flash(message, "error")
        return redirect(url_for("sales.index"))

    if request.is_json:
        return {"id": sale.id, "total": str(sale.total),
                "change_due": str(sale.change_due),
                "invoice_url": url_for("sales.invoice", sale_id=sale.id)}, 201
    flash(f"Sale #{sale.id} completed.", "success")
    return redirect(url_for("sales.invoice", sale_id=sale.id))


@sales_bp.get("/<int:sale_id>/invoice")
@access_required("sales")
def invoice(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    _ensure_sale_visible(sale)
    return render_template("invoice.html", sale=sale)


@sales_bp.get("/<int:sale_id>/invoice.pdf")
@access_required("sales")
def invoice_pdf(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    _ensure_sale_visible(sale)
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=letter)
    styles = getSampleStyleSheet()
    font_name, bold_font_name = _peso_pdf_fonts()
    styles["Title"].fontName = font_name
    styles["Normal"].fontName = font_name
    content = [
        Paragraph("RetailFlow — Invoice", styles["Title"]),
        Paragraph(
            f"Invoice #{sale.id} · {sale.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
            styles["Normal"],
        ),
        Spacer(1, 18),
    ]
    rows = [["Product", "Qty", "Unit price", "Amount"]]
    for item in sale.items:
        variant = " · ".join(
            value for value in (item.product_size, item.product_color) if value
        )
        label = item.product_name + (f" ({variant})" if variant else "")
        rows.append([
            Paragraph(label, styles["Normal"]),
            str(item.quantity),
            f"₱{item.unit_price:.2f}",
            f"₱{item.unit_price * item.quantity:.2f}",
        ])
    rows.append(["", "", "Total", f"₱{sale.total:.2f}"])
    rows.append(["", "", "Cash tendered", f"₱{sale.amount_tendered:.2f}"])
    rows.append(["", "", "Change due", f"₱{sale.change_due:.2f}"])
    table = Table(rows, colWidths=[240, 50, 85, 85])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172554")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTNAME", (2, -3), (-1, -1), bold_font_name),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    content.append(table)
    document.build(content)
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    filename = f"retailflow-invoice-{sale.id}.pdf"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def _ensure_sale_visible(sale):
    if (
        g.current_user.role != "admin"
        and not g.current_user.allows("sales.view_all")
        and sale.created_by_id != g.current_user.id
    ):
        abort(404)
