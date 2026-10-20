import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import limiter
from models import Order, OrderItem, Product, Sale, SaleItem, db
from security import (
    access_required,
    audit_event,
    discard_invalid_auth_cookies,
    optional_identity,
)

shop_bp = Blueprint("shop", __name__)
MAX_CART_ITEMS = 20
MAX_ITEM_QUANTITY = 99
ORDER_STATUSES = ("pending", "processing", "shipped", "delivered", "cancelled")
STATUS_LABELS = {
    "pending": "Pending",
    "processing": "Processing",
    "shipped": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


class InventoryConflict(ValueError):
    pass


def _guest_only(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if optional_identity() is not None:
            abort(403)
        g.current_user = None
        g.csrf_token = session.setdefault(
            "guest_csrf",
            secrets.token_urlsafe(32),
        )
        g.cart_count = sum(_read_cart().values())
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = (
                request.headers.get("X-CSRF-TOKEN")
                or request.form.get("csrf_token", "")
            )
            if not hmac.compare_digest(g.csrf_token, supplied):
                abort(400, description="Your form expired. Please try again.")
        return discard_invalid_auth_cookies(view(*args, **kwargs))

    return wrapped


def _read_cart():
    raw = session.get("cart", {})
    if not isinstance(raw, dict):
        return {}
    cart = {}
    for raw_id, raw_quantity in list(raw.items())[:MAX_CART_ITEMS]:
        try:
            product_id = int(raw_id)
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and 1 <= quantity <= MAX_ITEM_QUANTITY:
            cart[str(product_id)] = quantity
    if cart != raw:
        session["cart"] = cart
    return cart


def _cart_product(product_id):
    return Product.query.filter(
        Product.id == product_id,
        Product.is_active.is_(True),
    ).first()


def _cart_entries(cart):
    entries = []
    subtotal = Decimal("0.00")
    for raw_id, quantity in cart.items():
        product = _cart_product(int(raw_id))
        if product is None:
            entries.append({
                "product": None,
                "product_id": int(raw_id),
                "quantity": quantity,
                "line_total": Decimal("0.00"),
            })
            continue
        line_total = (product.price * quantity).quantize(Decimal("0.01"))
        subtotal += line_total
        entries.append({
            "product": product,
            "product_id": product.id,
            "quantity": quantity,
            "line_total": line_total,
        })
    return entries, subtotal.quantize(Decimal("0.01"))


def _invalidate_checkout():
    session.pop("checkout_key", None)
    session.pop("checkout_intent", None)


def _clear_submitted_checkout(key, payload, intent):
    if (
        session.get("checkout_key") != key
        or session.get("checkout_intent") != payload
    ):
        return
    if intent["source"] == "cart":
        session["cart"] = {}
    else:
        session.pop("buy_now", None)
    _invalidate_checkout()


def _valid_quantity(raw):
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid quantity.")
    if not 1 <= quantity <= MAX_ITEM_QUANTITY:
        raise ValueError("Quantity must be between 1 and 99.")
    return quantity


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sign_intent(key, payload):
    message = f"{key}.{payload}".encode("utf-8")
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _parse_intent(payload):
    if not payload or len(payload) > 2000:
        raise ValueError("Your checkout form expired. Please review your cart.")
    try:
        intent = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Your checkout form is invalid. Please review your cart.") from error
    if (
        not isinstance(intent, dict)
        or set(intent) != {"source", "items"}
        or intent["source"] not in {"cart", "buy_now"}
        or not isinstance(intent["items"], list)
        or not 1 <= len(intent["items"]) <= MAX_CART_ITEMS
    ):
        raise ValueError("Your checkout form is invalid. Please review your cart.")
    parsed, seen = [], set()
    for item in intent["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "quantity"}:
            raise ValueError("Your checkout form is invalid. Please review your cart.")
        product_id = item["id"]
        quantity = item["quantity"]
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id < 1
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 1 <= quantity <= MAX_ITEM_QUANTITY
            or product_id in seen
        ):
            raise ValueError("Your checkout form is invalid. Please review your cart.")
        seen.add(product_id)
        parsed.append({"id": product_id, "quantity": quantity})
    if intent["source"] == "buy_now" and len(parsed) != 1:
        raise ValueError("Your checkout form is invalid. Please review your cart.")
    return {"source": intent["source"], "items": parsed}


def _checkout_digest(intent, customer):
    content = {"intent": intent, "customer": customer}
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(
        secret,
        _canonical_json(content).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _customer_fields(form):
    name = form.get("customer_name", "").strip()
    email = form.get("customer_email", "").strip().lower()
    phone = form.get("customer_phone", "").strip()
    address = form.get("delivery_address", "").strip()
    note = form.get("customer_note", "").strip()
    if not name or len(name) > 120:
        raise ValueError("Enter your name (up to 120 characters).")
    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        raise ValueError("Enter a valid email address.")
    digits = sum(character.isdigit() for character in phone)
    if (
        len(phone) > 32
        or digits < 7
        or not re.fullmatch(r"[0-9+()\- ]+", phone)
    ):
        raise ValueError("Enter a valid phone number.")
    if not address or len(address) < 8 or len(address) > 500:
        raise ValueError("Enter a delivery address between 8 and 500 characters.")
    if len(note) > 500:
        raise ValueError("Order notes must be 500 characters or fewer.")
    return {
        "name": name,
        "email": email,
        "phone": phone,
        "address": address,
        "note": note,
    }


def _checkout_error(message):
    flash(message, "error")
    return redirect(url_for("shop.checkout"), code=303)


@shop_bp.get("/cart/")
@_guest_only
def cart():
    entries, subtotal = _cart_entries(_read_cart())
    return render_template(
        "cart.html",
        entries=entries,
        subtotal=subtotal,
        csrf_token=g.csrf_token,
    )


@shop_bp.post("/cart/add/<int:product_id>/")
@limiter.limit("60 per hour")
@_guest_only
def add_to_cart(product_id):
    product = _cart_product(product_id)
    if product is None:
        abort(404)
    try:
        quantity = _valid_quantity(request.form.get("quantity", "1"))
        cart_items = _read_cart()
        new_quantity = cart_items.get(str(product.id), 0) + quantity
        if new_quantity > MAX_ITEM_QUANTITY or new_quantity > product.quantity:
            raise ValueError(f"Not enough stock is available for {product.name}.")
        if str(product.id) not in cart_items and len(cart_items) >= MAX_CART_ITEMS:
            raise ValueError("Your cart can contain up to 20 different products.")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("products.view_product", product_id=product.id))
    cart_items[str(product.id)] = new_quantity
    session["cart"] = cart_items
    _invalidate_checkout()
    flash(f"{product.name} added to your bag.", "success")
    return redirect(url_for("products.view_product", product_id=product.id))


@shop_bp.post("/cart/items/<int:product_id>/update/")
@limiter.limit("60 per hour")
@_guest_only
def update_cart_item(product_id):
    cart_items = _read_cart()
    if str(product_id) not in cart_items:
        abort(404)
    product = _cart_product(product_id)
    try:
        quantity = _valid_quantity(request.form.get("quantity", ""))
        if product is None:
            raise ValueError("This product is no longer available.")
        if quantity > product.quantity:
            raise ValueError(f"Not enough stock is available for {product.name}.")
        cart_items[str(product_id)] = quantity
        session["cart"] = cart_items
        _invalidate_checkout()
        flash("Cart updated.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("shop.cart"))


@shop_bp.post("/cart/items/<int:product_id>/remove/")
@limiter.limit("60 per hour")
@_guest_only
def remove_cart_item(product_id):
    cart_items = _read_cart()
    cart_items.pop(str(product_id), None)
    session["cart"] = cart_items
    _invalidate_checkout()
    flash("Item removed from your bag.", "success")
    return redirect(url_for("shop.cart"))


@shop_bp.post("/buy-now/<int:product_id>/")
@limiter.limit("30 per hour")
@_guest_only
def buy_now(product_id):
    product = _cart_product(product_id)
    if product is None:
        abort(404)
    try:
        quantity = _valid_quantity(request.form.get("quantity", "1"))
        if quantity > product.quantity:
            raise ValueError(f"Not enough stock is available for {product.name}.")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("products.view_product", product_id=product.id))
    session["buy_now"] = {"product_id": product.id, "quantity": quantity}
    session.pop("checkout_key", None)
    session.pop("checkout_intent", None)
    return redirect(url_for("shop.checkout"))


@shop_bp.get("/checkout/")
@_guest_only
def checkout():
    buy_now_item = session.get("buy_now")
    if (
        isinstance(buy_now_item, dict)
        and isinstance(buy_now_item.get("product_id"), int)
        and isinstance(buy_now_item.get("quantity"), int)
    ):
        source = "buy_now"
        raw_items = [{
            "id": buy_now_item["product_id"],
            "quantity": buy_now_item["quantity"],
        }]
    else:
        source = "cart"
        raw_items = [
            {"id": int(product_id), "quantity": quantity}
            for product_id, quantity in _read_cart().items()
        ]
    if not raw_items:
        flash("Your bag is empty.", "error")
        return redirect(url_for("shop.cart"))

    intent = _parse_intent(_canonical_json({"source": source, "items": raw_items}))
    payload = _canonical_json(intent)
    if session.get("checkout_intent") != payload or not session.get("checkout_key"):
        session["checkout_key"] = secrets.token_urlsafe(32)
        session["checkout_intent"] = payload
    key = session["checkout_key"]
    entries = []
    subtotal = Decimal("0.00")
    for item in intent["items"]:
        product = _cart_product(item["id"])
        if product is None:
            if source == "buy_now":
                session.pop("buy_now", None)
                flash("This product is no longer available.", "error")
                return redirect(url_for("products.index"))
            flash("A product in your bag is no longer available.", "error")
            return redirect(url_for("shop.cart"))
        if item["quantity"] > product.quantity:
            if source == "buy_now":
                session.pop("buy_now", None)
                flash(f"Not enough stock is available for {product.name}.", "error")
                return redirect(
                    url_for("products.view_product", product_id=product.id)
                )
            flash(f"Not enough stock is available for {product.name}.", "error")
            return redirect(url_for("shop.cart"))
        line_total = (product.price * item["quantity"]).quantize(Decimal("0.01"))
        subtotal += line_total
        entries.append({
            "product": product,
            "quantity": item["quantity"],
            "line_total": line_total,
        })
    return render_template(
        "checkout.html",
        entries=entries,
        subtotal=subtotal.quantize(Decimal("0.01")),
        delivery_fee=Decimal("0.00"),
        total=subtotal.quantize(Decimal("0.01")),
        checkout_key=key,
        intent_payload=payload,
        intent_signature=_sign_intent(key, payload),
        csrf_token=g.csrf_token,
    )


@shop_bp.post("/checkout/")
@limiter.limit("10 per hour")
@_guest_only
def place_order():
    key = request.form.get("idempotency_key", "").strip()
    payload = request.form.get("intent_payload", "")
    signature = request.form.get("intent_signature", "")
    if not key or len(key) > 64 or not hmac.compare_digest(
        _sign_intent(key, payload),
        signature,
    ):
        return _checkout_error("Your checkout form expired. Please review your bag.")
    try:
        intent = _parse_intent(payload)
        customer = _customer_fields(request.form)
    except ValueError as error:
        return _checkout_error(str(error))
    digest = _checkout_digest(intent, customer)
    existing = Order.query.filter_by(idempotency_key=key).first()
    if existing:
        if not hmac.compare_digest(existing.request_digest, digest):
            if (
                session.get("checkout_key") == key
                and session.get("checkout_intent") == payload
            ):
                _invalidate_checkout()
            return _checkout_error(
                "This checkout was already submitted with different details. "
                "Please start a new checkout."
            )

        _clear_submitted_checkout(key, payload, intent)
        return redirect(
            url_for("shop.order_confirmation", reference=existing.public_reference),
            code=303,
        )

    ids = [item["id"] for item in intent["items"]]
    products = {
        product.id: product
        for product in Product.query.filter(
            Product.id.in_(ids),
            Product.is_active.is_(True),
        ).all()
    }
    if len(products) != len(ids):
        return _checkout_error("A product in your bag is no longer available.")

    try:
        subtotal = Decimal("0.00")
        order = Order(
            public_reference=Order.new_public_reference(),
            idempotency_key=key,
            request_digest=digest,
            customer_name=customer["name"],
            customer_email=customer["email"],
            customer_phone=customer["phone"],
            delivery_address=customer["address"],
            customer_note=customer["note"] or None,
            status="pending",
            payment_status="unpaid",
            subtotal=Decimal("0.00"),
            delivery_fee=Decimal("0.00"),
            total=Decimal("0.00"),
        )
        for item in intent["items"]:
            product = products[item["id"]]
            quantity = item["quantity"]
            changed = Product.query.filter(
                Product.id == product.id,
                Product.is_active.is_(True),
                Product.quantity >= quantity,
            ).update(
                {Product.quantity: Product.quantity - quantity},
                synchronize_session=False,
            )
            if changed != 1:
                raise InventoryConflict(
                    f"Not enough stock is available for {product.name}. "
                    "Please review your bag."
                )
            unit_price = Decimal(product.price).quantize(Decimal("0.01"))
            unit_cost = Decimal(product.cost).quantize(Decimal("0.01"))
            subtotal += unit_price * quantity
            order.items.append(OrderItem(
                product=product,
                product_name=product.name,
                product_sku=product.sku,
                product_category=product.category,
                product_size=product.size,
                product_color=product.color,
                quantity=quantity,
                unit_price=unit_price,
                unit_cost=unit_cost,
            ))
        order.subtotal = subtotal.quantize(Decimal("0.01"))
        order.total = order.subtotal
        db.session.add(order)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Order.query.filter_by(idempotency_key=key).first()
        if existing and hmac.compare_digest(existing.request_digest, digest):
            _clear_submitted_checkout(key, payload, intent)
            return redirect(
                url_for("shop.order_confirmation", reference=existing.public_reference),
                code=303,
            )
        raise
    except OperationalError as error:
        db.session.rollback()
        if "locked" not in str(error).lower():
            raise
        existing = Order.query.filter_by(idempotency_key=key).first()
        if existing and hmac.compare_digest(existing.request_digest, digest):
            _clear_submitted_checkout(key, payload, intent)
            return redirect(
                url_for("shop.order_confirmation", reference=existing.public_reference),
                code=303,
            )
        return _checkout_error(
            "Inventory changed while placing your order. Please review your bag.",
        )
    except InventoryConflict as error:
        db.session.rollback()
        existing = Order.query.filter_by(idempotency_key=key).first()
        if existing and hmac.compare_digest(existing.request_digest, digest):
            _clear_submitted_checkout(key, payload, intent)
            return redirect(
                url_for("shop.order_confirmation", reference=existing.public_reference),
                code=303,
            )
        return _checkout_error(str(error))
    except ValueError as error:
        db.session.rollback()
        return _checkout_error(str(error))

    _clear_submitted_checkout(key, payload, intent)
    return redirect(
        url_for("shop.order_confirmation", reference=order.public_reference),
        code=303,
    )


@shop_bp.get("/orders/<string:reference>/confirmation/")
@_guest_only
def order_confirmation(reference):
    if not re.fullmatch(r"[A-Za-z0-9_-]{30,48}", reference):
        abort(404)
    order = Order.query.filter_by(public_reference=reference).first_or_404()
    response = current_app.make_response(render_template(
        "order_confirmation.html",
        order=order,
        status_label=STATUS_LABELS[order.status],
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@shop_bp.get("/admin/orders/")
@access_required(admin=True)
def admin_orders():
    status = request.args.get("status", "")
    if status and status not in ORDER_STATUSES:
        abort(400, description="Choose a supported order status.")
    query = Order.query
    if status:
        query = query.filter_by(status=status)
    orders = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(250).all()
    return render_template(
        "admin_orders.html",
        orders=orders,
        statuses=ORDER_STATUSES,
        status_labels=STATUS_LABELS,
        selected_status=status,
    )


@shop_bp.get("/admin/orders/<int:order_id>/")
@access_required(admin=True)
def admin_order_detail(order_id):
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    return render_template(
        "admin_order_detail.html",
        order=order,
        status_labels=STATUS_LABELS,
        next_statuses=_next_statuses(order.status),
    )


def _next_statuses(status):
    if status == "pending":
        return ("processing", "cancelled")
    if status == "processing":
        return ("shipped", "cancelled")
    if status == "shipped":
        return ("delivered", "cancelled")
    return ()


@shop_bp.post("/admin/orders/<int:order_id>/")
@access_required(admin=True)
def update_admin_order(order_id):
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    new_status = request.form.get("status", order.status)
    collect_payment = request.form.get("payment_status") == "collected"
    if new_status not in ORDER_STATUSES:
        abort(400, description="Choose a supported order status.")
    if new_status != order.status and new_status not in _next_statuses(order.status):
        flash("That order status transition is not allowed.", "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))
    if order.payment_status == "collected" and not collect_payment:
        flash("Collected COD payment cannot be reversed.", "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))
    if collect_payment and new_status != "delivered":
        flash("COD can be collected only after the order is delivered.", "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))
    if (
        new_status == "cancelled"
        and (order.status == "delivered" or order.payment_status == "collected")
    ):
        flash("A delivered or paid order cannot be cancelled.", "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))

    old_status = order.status
    old_payment_status = order.payment_status
    try:
        if (
            new_status == old_status
            and (not collect_payment or old_payment_status == "collected")
        ):
            flash("No order changes to save.", "info")
            return redirect(url_for("shop.admin_order_detail", order_id=order.id))
        next_payment_status = (
            "collected" if collect_payment else old_payment_status
        )
        changed = Order.query.filter(
            Order.id == order.id,
            Order.status == old_status,
            Order.payment_status == old_payment_status,
        ).update(
            {
                Order.status: new_status,
                Order.payment_status: next_payment_status,
                Order.updated_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        if changed != 1:
            db.session.rollback()
            flash("This order changed in another request. Reload and try again.", "error")
            return redirect(url_for("shop.admin_order_detail", order_id=order.id))
        db.session.expire(order)
        order = db.session.get(Order, order.id)
        if new_status == "cancelled" and old_status != "cancelled":
            for item in order.items:
                if item.product_id is not None:
                    Product.query.filter_by(id=item.product_id).update(
                        {Product.quantity: Product.quantity + item.quantity},
                        synchronize_session=False,
                    )
        if order.payment_status == "collected" and order.sale is None:
            sale = Sale(
                total=order.total,
                amount_tendered=order.total,
                status="completed",
                order=order,
            )
            db.session.add(sale)
            for item in order.items:
                if item.product_id is None:
                    raise ValueError(
                        "A product from this order is no longer available, so its "
                        "sale cannot be recorded."
                    )
                sale.items.append(SaleItem(
                    product_id=item.product_id,
                    product_name=item.product_name,
                    product_size=item.product_size,
                    product_color=item.product_color,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    unit_cost=item.unit_cost,
                ))
        audit_event(
            "guest_order_updated",
            actor=g.current_user,
            details={
                "order_id": order.id,
                "old_status": old_status,
                "new_status": order.status,
                "payment_status": order.payment_status,
            },
        )
        db.session.commit()
    except OperationalError as error:
        db.session.rollback()
        if "locked" not in str(error).lower():
            raise
        flash("This order changed in another request. Reload and try again.", "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))
    except (IntegrityError, ValueError) as error:
        db.session.rollback()
        if isinstance(error, IntegrityError):
            raise
        flash(str(error), "error")
        return redirect(url_for("shop.admin_order_detail", order_id=order.id))

    flash("Order updated.", "success")
    return redirect(url_for("shop.admin_order_detail", order_id=order.id))
