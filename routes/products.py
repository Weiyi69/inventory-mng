import os
import re
from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

import barcode
from PIL import Image, ImageOps, UnidentifiedImageError
from barcode.writer import SVGWriter
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import RequestEntityTooLarge

from extensions import limiter
from models import CartItem, OrderItem, Product, SaleItem, db
from security import access_required, catalog_access

products_bp = Blueprint("products", __name__, url_prefix="/products")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000


def _save_product_image(upload):
    if not upload or not upload.filename:
        return None

    raw = upload.stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Product images must be 5 MB or smaller.")

    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Choose a JPG, PNG, or WebP product image.")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("Product image dimensions are too large.")
            source.verify()
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1200, 1200))
            output = BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("The uploaded file is not a valid product image.") from error

    filename = f"{uuid4().hex}.jpg"
    upload_folder = os.path.join(current_app.instance_path, "uploads", "products")
    os.makedirs(upload_folder, exist_ok=True)
    with open(os.path.join(upload_folder, filename), "wb") as image_file:
        image_file.write(output.getvalue())
    return filename


def _money(value, field_name):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Enter a valid {field_name}.")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field_name.capitalize()} must be a non-negative amount.")
    return amount.quantize(Decimal("0.01"))


@products_bp.get("/")
@catalog_access
def index():
    query = request.args.get("q", "").strip()
    is_catalog_user = (
        g.current_user is None or g.current_user.role != "admin"
    )
    show_archived = (
        request.args.get("archived") == "1"
        if not is_catalog_user
        else False
    )
    products = Product.query.filter(Product.is_active.is_(not show_archived))
    categories = [
        row[0]
        for row in db.session.query(Product.category)
        .filter(Product.is_active.is_(True))
        .distinct()
        .order_by(Product.category)
        .all()
    ]
    selected_category = request.args.get("category", "").strip()
    if query:
        products = products.filter(
            or_(
                Product.name.ilike(f"%{query}%"),
                Product.sku.ilike(f"%{query}%"),
                Product.barcode.ilike(f"%{query}%"),
                Product.category.ilike(f"%{query}%"),
                Product.size.ilike(f"%{query}%"),
                Product.color.ilike(f"%{query}%"),
            )
        )
    if selected_category in categories:
        products = products.filter(Product.category == selected_category)
    elif selected_category:
        selected_category = ""
    if is_catalog_user:
        return render_template(
            "products_catalog.html",
            products=products.order_by(Product.name).all(),
            search=query,
            categories=categories,
            selected_category=selected_category,
        )
    return render_template(
        "products.html",
        products=products.order_by(Product.name).all(),
        search=query,
        show_archived=show_archived,
        active_count=Product.query.filter_by(is_active=True).count(),
        archived_count=Product.query.filter_by(is_active=False).count(),
        can_write=g.current_user.allows("products.write"),
        can_delete=g.current_user.allows("products.delete"),
        can_restock=g.current_user.allows("products.restock"),
        can_view_costs=g.current_user.allows("products.costs"),
    )


@products_bp.get("/<int:product_id>/view/")
@catalog_access
def view_product(product_id):
    product = Product.query.filter(
        Product.id == product_id,
        Product.is_active.is_(True),
    ).first_or_404()
    return render_template(
        "product_detail.html",
        product=product,
        can_view_stock=(
            g.current_user is not None
            and (
                g.current_user.role == "admin"
                or g.current_user.allows("products.stock_view")
            )
        ),
    )


@products_bp.get("/<int:product_id>/edit")
@access_required("products", "products.write")
def edit(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        flash("Restore this product before editing it.", "error")
        return redirect(url_for("products.index", archived=1))
    return render_template(
        "product_edit.html",
        product=product,
        can_view_costs=g.current_user.allows("products.costs"),
    )


@products_bp.post("/<int:product_id>/edit")
@access_required("products", "products.write")
def update(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        flash("Restore this product before editing it.", "error")
        return redirect(url_for("products.index", archived=1))

    new_image_filename = None
    previous_image_filename = product.image_filename
    try:
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip()
        barcode_value = request.form.get("barcode", "").strip() or sku
        category = request.form.get("category", "").strip() or "General"
        size = request.form.get("size", "").strip() or None
        color = request.form.get("color", "").strip() or None
        threshold = int(request.form.get("low_stock_threshold", ""))
        if not name or len(name) > 120:
            raise ValueError("Product name must contain 1 to 120 characters.")
        if not sku or len(sku) > 64:
            raise ValueError("SKU must contain 1 to 64 characters.")
        if len(barcode_value) > 64:
            raise ValueError("Barcode must be 64 characters or fewer.")
        if len(category) > 80:
            raise ValueError("Category must be 80 characters or fewer.")
        if size and len(size) > 32:
            raise ValueError("Size must be 32 characters or fewer.")
        if color and len(color) > 48:
            raise ValueError("Color must be 48 characters or fewer.")
        if threshold < 0:
            raise ValueError("Low-stock threshold cannot be negative.")

        new_image_filename = _save_product_image(request.files.get("image"))
        product.name = name
        product.sku = sku
        product.barcode = barcode_value
        product.category = category
        product.size = size
        product.color = color
        product.price = _money(request.form.get("price"), "price")
        if g.current_user.allows("products.costs"):
            product.cost = _money(
                request.form.get("cost", str(product.cost)),
                "cost",
            )
        product.low_stock_threshold = threshold
        if new_image_filename:
            product.image_filename = new_image_filename
        db.session.commit()
    except (ValueError, TypeError, RequestEntityTooLarge) as error:
        db.session.rollback()
        if new_image_filename:
            _delete_product_image(new_image_filename)
        flash(str(error), "error")
        return redirect(url_for("products.edit", product_id=product.id))
    except IntegrityError:
        db.session.rollback()
        if new_image_filename:
            _delete_product_image(new_image_filename)
        flash("SKU and barcode must each be unique.", "error")
        return redirect(url_for("products.edit", product_id=product.id))

    if new_image_filename and previous_image_filename:
        _delete_product_image(previous_image_filename)
    flash(f"{product.name} was updated.", "success")
    return redirect(url_for("products.index"))


@products_bp.post("/")
@access_required("products", "products.write")
def create():
    image_filename = None
    try:
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip()
        if not name or not sku:
            raise ValueError("Product name and SKU are required.")
        quantity = int(request.form.get("quantity", "0"))
        threshold = int(request.form.get("low_stock_threshold", "5"))
        if quantity < 0 or threshold < 0:
            raise ValueError("Stock quantities cannot be negative.")
        image_filename = _save_product_image(request.files.get("image"))
        product = Product(
            name=name,
            sku=sku,
            barcode=request.form.get("barcode", "").strip() or sku,
            category=request.form.get("category", "").strip() or "General",
            size=request.form.get("size", "").strip() or None,
            color=request.form.get("color", "").strip() or None,
            image_filename=image_filename,
            price=_money(request.form.get("price"), "price"),
            cost=(
                _money(request.form.get("cost", "0"), "cost")
                if g.current_user.allows("products.costs")
                else Decimal("0.00")
            ),
            quantity=quantity,
            low_stock_threshold=threshold,
        )
        db.session.add(product)
        db.session.commit()
    except (ValueError, TypeError, RequestEntityTooLarge) as error:
        db.session.rollback()
        if image_filename:
            _delete_product_image(image_filename)
        flash(str(error), "error")
    except IntegrityError:
        db.session.rollback()
        if image_filename:
            _delete_product_image(image_filename)
        flash("SKU and barcode must each be unique.", "error")
    else:
        flash("Product added.", "success")
    return redirect(url_for("products.index"))


def _delete_product_image(filename):
    image_path = os.path.join(
        current_app.instance_path,
        "uploads",
        "products",
        filename,
    )
    if os.path.isfile(image_path):
        os.remove(image_path)


@products_bp.post("/<int:product_id>/delete")
@access_required("products", "products.delete")
def delete(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        flash("This product is already archived.", "error")
        return redirect(url_for("products.index", archived=1))

    has_sales = SaleItem.query.filter_by(product_id=product.id).first() is not None
    has_orders = OrderItem.query.filter_by(product_id=product.id).first() is not None
    if has_sales or has_orders:
        product.is_active = False
        db.session.commit()
        flash(
            f"{product.name} has order or sales history, so it was archived "
            "instead of deleted. Existing records are preserved.",
            "success",
        )
    else:
        image_filename = product.image_filename
        CartItem.query.filter_by(product_id=product.id).delete(
            synchronize_session=False,
        )
        db.session.delete(product)
        db.session.commit()
        if image_filename:
            _delete_product_image(image_filename)
        flash(f"{product.name} was permanently deleted.", "success")
    return redirect(url_for("products.index"))


@products_bp.post("/<int:product_id>/restore")
@access_required("products", "products.delete")
def restore(product_id):
    product = Product.query.get_or_404(product_id)
    if product.is_active:
        flash("This product is already active.", "error")
        return redirect(url_for("products.index"))
    product.is_active = True
    db.session.commit()
    flash(f"{product.name} was restored to active inventory.", "success")
    return redirect(url_for("products.index", archived=1))


@products_bp.get("/images/<string:filename>")
@limiter.exempt
@catalog_access
def product_image(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.jpg", filename):
        return "", 404
    if g.current_user is None or g.current_user.role != "admin":
        if not Product.query.filter_by(
            image_filename=filename,
            is_active=True,
        ).first():
            return "", 404
    return send_from_directory(
        os.path.join(current_app.instance_path, "uploads", "products"),
        filename,
    )


@products_bp.post("/<int:product_id>/stock")
@access_required("products", "products.restock")
def restock(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        flash("Archived products must be restored before restocking.", "error")
        return redirect(url_for("products.index", archived=1))
    try:
        quantity = int(request.form.get("quantity", ""))
        if quantity < 1:
            raise ValueError("Restock quantity must be at least 1.")
        product.quantity += quantity
        db.session.commit()
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        flash(f"Added {quantity} units to {product.name}.", "success")
    return redirect(url_for("products.index"))


@products_bp.get("/<int:product_id>/barcode.svg")
@access_required("products")
def barcode_svg(product_id):
    product = Product.query.get_or_404(product_id)
    barcode_class = barcode.get_barcode_class("code128")
    image = BytesIO()
    barcode_class(product.barcode, writer=SVGWriter()).write(
        image,
        options={"write_text": True},
    )
    response = make_response(image.getvalue())
    response.headers["Content-Type"] = "image/svg+xml"
    return response
