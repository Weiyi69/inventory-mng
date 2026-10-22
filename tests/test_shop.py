import html
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app import create_app
from config import Config
from models import CartItem, Order, Product, Sale, SaleItem, User, db


class ShopTestConfig(Config):
    TESTING = True
    APP_ENV = "test"
    SECRET_KEY = "test-secret-key-for-retailflow-guest-shop"
    JWT_SECRET_KEY = "test-jwt-secret-key-for-retailflow-guest-shop"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


@pytest.fixture
def shop_app(tmp_path):
    class TestConfig(ShopTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'shop-test.db'}"

    app = create_app(TestConfig)
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.create_all()
        admin = User(name="Admin", email="admin@example.test", role="admin")
        admin.set_password("correct-horse-battery")
        user = User(name="Staff", email="staff@example.test", role="user")
        user.set_password("correct-horse-battery")
        first = Product(
            name="Navy Hoodie",
            sku="HOOD-NAVY",
            barcode="100001",
            category="Hoodies",
            price=Decimal("1250.00"),
            cost=Decimal("500.00"),
            quantity=5,
            low_stock_threshold=1,
        )
        second = Product(
            name="Cotton Tee",
            sku="TEE-COTTON",
            barcode="100002",
            category="Tee",
            price=Decimal("600.00"),
            cost=Decimal("200.00"),
            quantity=3,
            low_stock_threshold=1,
        )
        db.session.add_all([admin, user, first, second])
        db.session.commit()
    yield app
    with app.app_context():
        db.drop_all()


def _csrf(response):
    match = re.search(rb'<meta name="csrf-token" content="([^"]*)"', response.data)
    if not match:
        match = re.search(rb'name="csrf_token" value="([^"]*)"', response.data)
    assert match
    return html.unescape(match.group(1).decode())


def _post(client, path, data=None, **kwargs):
    data = dict(data or {})
    data.setdefault("csrf_token", _csrf(client.get("/cart/")))
    return client.post(path, data=data, **kwargs)


def _checkout_form(client):
    page = client.get("/checkout/")
    assert page.status_code == 200
    fields = {}
    for name in (
        "csrf_token",
        "idempotency_key",
        "intent_payload",
        "intent_signature",
    ):
        match = re.search(
            rf'name="{name}" value="([^"]*)"'.encode(),
            page.data,
        )
        assert match, name
        fields[name] = html.unescape(match.group(1).decode())
    fields.update({
        "customer_name": "Casey Customer",
        "customer_email": "casey@example.test",
        "customer_phone": "+63 912 345 6789",
        "delivery_address": "123 Example Street, Quezon City",
        "customer_note": "",
    })
    return fields


def _login(client, email):
    response = client.get("/login")
    csrf = _csrf(response)
    return client.post("/login", data={
        "csrf_token": csrf,
        "email": email,
        "password": "correct-horse-battery",
    })


def test_public_catalog_detail_and_product_images_are_available(shop_app):
    image_bytes = BytesIO()
    Image.new("RGB", (30, 40), color="navy").save(image_bytes, format="JPEG")
    image_dir = Path(shop_app.instance_path) / "uploads" / "products"
    image_dir.mkdir(parents=True, exist_ok=True)
    filename = "d" * 32 + ".jpg"
    (image_dir / filename).write_bytes(image_bytes.getvalue())
    with shop_app.app_context():
        product = db.session.get(Product, 1)
        product.image_filename = filename
        db.session.commit()

    with shop_app.test_client() as client:
        catalog = client.get("/products/")
        detail = client.get("/products/1/view/")
        cart = client.get("/cart/")
        image = client.get(f"/products/images/{filename}")
        missing_product = client.get("/products/999/view/")

    assert catalog.status_code == detail.status_code == cart.status_code == image.status_code == 200
    assert b"Navy Hoodie" in catalog.data and "₱1,250.00".encode() in catalog.data
    assert b"cost" not in catalog.data.lower()
    for page in (catalog, detail, cart):
        assert b'class="catalog-header-icon"' in page.data
        assert b'<path d="m2.05 2.05 1.099' in page.data
        assert b"Bag" not in page.data
        assert b'aria-label="Shopping cart, 0 items"' in page.data
        assert b'<span aria-hidden="true">0</span>' in page.data
    for page in (catalog, detail):
        assert b'aria-label="Sign in"' in page.data
        assert b'href="/login?next=' in page.data
        assert b'<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>' in page.data
    assert b"Add to cart" in detail.data and b"Buy it now" in detail.data
    assert detail.data.count(b'data-quantity-stepper') == 1
    assert detail.data.count(b'data-quantity-step="-1"') == 1
    assert detail.data.count(b'data-quantity-step="1"') == 1
    assert detail.data.count(b'name="quantity"') == 1
    assert b'max="5"' in detail.data
    assert b'action="/cart/add/1/"' in detail.data
    assert b'formaction="/buy-now/1/"' in detail.data
    assert b"Limited pieces" not in detail.data
    assert b'aria-label="Decrease quantity"' in detail.data
    assert b'aria-label="Increase quantity"' in detail.data
    assert b"color-swatch" not in detail.data.lower()
    assert image.mimetype == "image/jpeg"
    assert missing_product.status_code == 404


def test_product_images_are_not_blocked_by_default_rate_limit(tmp_path):
    class ImageRateLimitConfig(Config):
        TESTING = True
        APP_ENV = "test"
        SECRET_KEY = "test-secret-key-for-product-image-limit"
        JWT_SECRET_KEY = "test-jwt-secret-key-for-product-image-limit"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'image-limit-test.db'}"
        RATELIMIT_DEFAULT = "1 per hour"

    app = create_app(ImageRateLimitConfig)
    app.instance_path = str(tmp_path / "instance")
    filename = "e" * 32 + ".jpg"
    image_bytes = BytesIO()
    Image.new("RGB", (8, 8), color="navy").save(image_bytes, format="JPEG")
    image_dir = Path(app.instance_path) / "uploads" / "products"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / filename).write_bytes(image_bytes.getvalue())

    with app.app_context():
        db.create_all()
        db.session.add(Product(
            name="Rate limited image",
            sku="RATE-LIMIT-IMAGE",
            barcode="RATE-LIMIT-IMAGE",
            category="Clothing",
            price=10,
            cost=4,
            quantity=1,
            image_filename=filename,
        ))
        db.session.commit()

    try:
        with app.test_client() as client:
            image_url = f"/products/images/{filename}"
            responses = [client.get(image_url) for _ in range(2)]
        assert [response.status_code for response in responses] == [200, 200]
    finally:
        with app.app_context():
            db.drop_all()


def test_product_detail_shows_limited_pieces_at_low_stock(shop_app):
    with shop_app.app_context():
        product = db.session.get(Product, 1)
        product.quantity = 1
        product.low_stock_threshold = 1
        db.session.commit()

    with shop_app.test_client() as client:
        detail = client.get("/products/1/view/")

    assert detail.status_code == 200
    assert b"Limited pieces" in detail.data
    assert b'max="1"' in detail.data


def test_guest_cart_add_update_remove_and_csrf(shop_app):
    with shop_app.test_client() as client:
        csrf = _csrf(client.get("/products/"))
        bad = client.post("/cart/add/1/", data={"quantity": "1"})
        assert bad.status_code == 400
        added = client.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "2",
        })
        assert added.status_code == 302
        bag = client.get("/cart/")
        assert b"Navy Hoodie" in bag.data and b"2" in bag.data
        updated = _post(client, "/cart/items/1/update/", {"quantity": "3"})
        assert updated.status_code == 302
        assert b"3" in client.get("/cart/").data
        removed = _post(client, "/cart/items/1/remove/")
        assert removed.status_code == 302
        empty = client.get("/cart/")
        assert b"Your cart is empty" in empty.data
        assert b"Explore products" in empty.data


def test_standard_user_cart_persists_and_merges_guest_cart(shop_app):
    with shop_app.test_client() as account_client:
        assert _login(account_client, "staff@example.test").status_code == 302
        csrf = _csrf(account_client.get("/products/"))
        account_client.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "2",
        })

    with shop_app.test_client() as guest_client:
        csrf = _csrf(guest_client.get("/products/"))
        guest_client.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "1",
        })
        assert _login(guest_client, "staff@example.test").status_code == 302
        cart = guest_client.get("/cart/")
        assert b"Navy Hoodie" in cart.data
        assert "₱3,750.00".encode() in cart.data
        assert b"Shopping cart, 3 items" in cart.data
        assert b"guest cart was merged" in cart.data

    with shop_app.app_context():
        assert CartItem.query.one().quantity == 3

    with shop_app.test_client() as returning_client:
        assert _login(returning_client, "staff@example.test").status_code == 302
        catalog = returning_client.get("/products/")
        assert "₱3,750.00".encode() in returning_client.get("/cart/").data
        logout = returning_client.post(
            "/auth/logout",
            headers={"X-CSRF-TOKEN": _csrf(catalog)},
        )
        assert logout.status_code == 302
        assert logout.location == "/products/"
        guest_catalog = returning_client.get(logout.location)
        assert guest_catalog.status_code == 200
        assert b'aria-label="Sign in"' in guest_catalog.data
        assert b"Log out" not in guest_catalog.data
        assert b'aria-label="Shopping cart, 0 items"' in guest_catalog.data
        assert b"Your cart is empty" in returning_client.get("/cart/").data

    with shop_app.app_context():
        assert CartItem.query.one().quantity == 3


def test_standard_user_buy_now_isolated_and_orders_are_private(shop_app):
    with shop_app.test_client() as client:
        assert _login(client, "staff@example.test").status_code == 302
        csrf = _csrf(client.get("/products/"))
        added = client.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "1",
        })
        assert added.status_code == 302
        detail = client.get("/products/2/view/")
        assert b"Add to cart" in detail.data and b"Buy it now" in detail.data
        csrf = _csrf(detail)
        bought = client.post("/buy-now/2/", data={
            "csrf_token": csrf,
            "quantity": "2",
        })
        assert bought.status_code == 302
        checkout = client.get("/checkout/")
        assert b"Cotton Tee" in checkout.data
        assert b"Navy Hoodie" not in checkout.data
        form = _checkout_form(client)
        placed = client.post("/checkout/", data=form)
        assert placed.status_code == 303
        duplicate = client.post("/checkout/", data=form)
        assert duplicate.status_code == 303
        assert duplicate.location == placed.location
        confirmation = client.get(placed.location)
        assert confirmation.status_code == 200
        assert b"Cotton Tee" in confirmation.data
        assert "₱1,200.00".encode() in confirmation.data
        history = client.get("/orders/")
        assert b"Your orders" in history.data
        match = re.search(rb'href="(/orders/\d+/)"', history.data)
        assert match
        detail_page = client.get(match.group(1).decode())
        assert detail_page.status_code == 200
        assert b"Cotton Tee" in detail_page.data
        assert b"Navy Hoodie" in client.get("/cart/").data

    with shop_app.app_context():
        order = Order.query.one()
        owner_id = order.user_id
        order_id = order.id
        reference = order.public_reference
        assert owner_id is not None
        assert order.total == Decimal("1200.00")
        assert db.session.get(Product, 1).quantity == 5
        assert db.session.get(Product, 2).quantity == 1
        other_user = User(
            name="Another customer",
            email="another@example.test",
            role="user",
        )
        other_user.set_password("correct-horse-battery")
        db.session.add(other_user)
        db.session.commit()

    with shop_app.test_client() as other_client:
        assert _login(other_client, "another@example.test").status_code == 302
        assert other_client.get(f"/orders/{order_id}/").status_code == 404
        assert other_client.get(
            f"/orders/{reference}/confirmation/"
        ).status_code == 404


def test_shop_pages_redirect_to_sign_in_and_standard_users_cannot_administer(shop_app):
    with shop_app.test_client() as guest:
        response = guest.get("/orders/")
        assert response.status_code == 302
        assert response.location.startswith("/login?next=/orders/")
        login_page = guest.get(response.location)
        signed_in = guest.post(response.location, data={
            "csrf_token": _csrf(login_page),
            "email": "staff@example.test",
            "password": "correct-horse-battery",
        })
        assert signed_in.status_code == 302
        assert signed_in.location == "/orders/"

    with shop_app.test_client() as user:
        assert _login(user, "staff@example.test").status_code == 302
        csrf = _csrf(user.get("/products/"))
        assert user.get("/orders/").status_code == 200
        assert user.get("/admin/orders/").status_code == 403
        assert user.get("/expenses/").location == "/products/"
        assert user.get("/sales/").location == "/products/"
        oversold = user.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "6",
        })
        assert oversold.status_code == 302
        with shop_app.app_context():
            assert CartItem.query.count() == 0
        added = user.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "1",
        })
        assert added.status_code == 302, added.get_data(as_text=True)
        rejected = user.post("/cart/add/1/", data={
            "csrf_token": "invalid",
            "quantity": "1",
        })
        assert rejected.status_code == 302
        with shop_app.app_context():
            assert CartItem.query.one().quantity == 1


def test_buy_now_bypasses_cart_and_checkout_uses_current_database_prices(shop_app):
    with shop_app.test_client() as client:
        csrf = _csrf(client.get("/products/"))
        client.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "1"})
        client.post("/buy-now/2/", data={"csrf_token": csrf, "quantity": "2"})
        checkout_page = client.get("/checkout/")
        assert b"Cotton Tee" in checkout_page.data
        form = _checkout_form(client)
        form["total"] = "0.01"
        form["price"] = "0.01"
        placed = client.post("/checkout/", data=form)
        assert placed.status_code == 303
        confirmation = client.get(placed.location)
        assert b"Cotton Tee" in confirmation.data
        assert "₱1,200.00".encode() in confirmation.data
        assert "₱0.01".encode() not in confirmation.data
        assert "₱1,250.00".encode() in client.get("/cart/").data
    with shop_app.app_context():
        order = Order.query.one()
        assert order.total == Decimal("1200.00")
        assert order.payment_status == "unpaid"
        assert db.session.get(Product, 1).quantity == 5
        assert db.session.get(Product, 2).quantity == 1
        assert Sale.query.count() == 0


def test_cart_checkout_is_atomic_and_duplicate_submission_is_idempotent(shop_app):
    with shop_app.test_client() as client:
        csrf = _csrf(client.get("/products/"))
        client.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "1"})
        client.post("/cart/add/2/", data={"csrf_token": csrf, "quantity": "1"})
        form = _checkout_form(client)
        with shop_app.app_context():
            product = db.session.get(Product, 2)
            product.quantity = 0
            db.session.commit()
        failed = client.post("/checkout/", data=form)
        assert failed.status_code == 303
    with shop_app.app_context():
        assert db.session.get(Product, 1).quantity == 5
        assert db.session.get(Product, 2).quantity == 0
        assert Order.query.count() == 0

    with shop_app.test_client() as client:
        csrf = _csrf(client.get("/products/"))
        client.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "2"})
        form = _checkout_form(client)
        first = client.post("/checkout/", data=form)
        repeated = client.post("/checkout/", data=form)
        assert first.status_code == repeated.status_code == 303
        assert first.location == repeated.location
    with shop_app.app_context():
        assert Order.query.count() == 1
        assert db.session.get(Product, 1).quantity == 3


def test_competing_stale_checkouts_cannot_overdraw_inventory(shop_app):
    with shop_app.app_context():
        db.session.get(Product, 1).quantity = 1
        db.session.commit()
    clients = [shop_app.test_client(), shop_app.test_client()]
    forms = []
    for client in clients:
        csrf = _csrf(client.get("/products/"))
        client.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "1"})
        forms.append(_checkout_form(client))
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(
            lambda pair: pair[0].post("/checkout/", data=pair[1]),
            zip(clients, forms),
        ))
    assert sorted(response.status_code for response in responses) == [303, 303]
    with shop_app.app_context():
        assert db.session.get(Product, 1).quantity == 0
        assert Order.query.count() == 1


def test_invalid_checkout_inactive_product_and_tampered_intent_are_rejected(shop_app):
    with shop_app.test_client() as client:
        csrf = _csrf(client.get("/products/"))
        client.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "1"})
        form = _checkout_form(client)
        invalid = dict(form, customer_email="not-an-email")
        assert client.post("/checkout/", data=invalid).status_code == 303
        assert b"valid email" in client.get("/checkout/").data
        tampered = dict(form, intent_payload='{"source":"cart","items":[]}')
        assert client.post("/checkout/", data=tampered).status_code == 303
        with shop_app.app_context():
            product = db.session.get(Product, 1)
            product.is_active = False
            db.session.commit()
        unavailable = client.post("/checkout/", data=form)
        assert unavailable.status_code == 303
    with shop_app.app_context():
        assert Order.query.count() == 0
        assert db.session.get(Product, 1).quantity == 5


def test_admin_order_flow_records_sale_only_after_delivered_and_cod_collected(shop_app):
    with shop_app.test_client() as guest:
        csrf = _csrf(guest.get("/products/"))
        guest.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "2"})
        checkout = _checkout_form(guest)
        response = guest.post("/checkout/", data=checkout)
        assert response.status_code == 303

    with shop_app.test_client() as admin:
        assert _login(admin, "admin@example.test").status_code == 302
        assert b"Orders" in admin.get("/").data
        listing = admin.get("/admin/orders/")
        assert b"Casey Customer" in listing.data
        with shop_app.app_context():
            order_id = Order.query.one().id
        detail = admin.get(f"/admin/orders/{order_id}/")
        csrf = _csrf(detail)
        for status in ("processing", "shipped"):
            updated = admin.post(f"/admin/orders/{order_id}/", data={
                "csrf_token": csrf,
                "status": status,
            })
            assert updated.status_code == 302
        delivered = admin.post(f"/admin/orders/{order_id}/", data={
            "csrf_token": csrf,
            "status": "delivered",
        })
        assert delivered.status_code == 302
        with shop_app.app_context():
            assert Sale.query.count() == 0
            reserved_stock = db.session.get(Product, 1).quantity
        assert b"Navy Hoodie" not in admin.get("/reports/?range=all").data
        collected = admin.post(f"/admin/orders/{order_id}/", data={
            "csrf_token": csrf,
            "status": "delivered",
            "payment_status": "collected",
        })
        assert collected.status_code == 302
        with shop_app.app_context():
            order = db.session.get(Order, order_id)
            sale = Sale.query.one()
            item = SaleItem.query.one()
            assert order.payment_status == "collected"
            assert order.sale.id == sale.id
            assert sale.total == order.total == Decimal("2500.00")
            assert (item.quantity, item.unit_cost) == (2, Decimal("500.00"))
            assert db.session.get(Product, 1).quantity == reserved_stock
        report = admin.get("/reports/?range=all")
        assert b"Navy Hoodie" in report.data
        assert b"2500" in report.data
        admin.post(f"/admin/orders/{order_id}/", data={
            "csrf_token": csrf,
            "status": "delivered",
            "payment_status": "collected",
        })
    with shop_app.app_context():
        assert Sale.query.count() == 1


def test_cancellation_restores_reserved_stock_once_and_orders_are_admin_only(shop_app):
    with shop_app.test_client() as guest:
        csrf = _csrf(guest.get("/products/"))
        guest.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "2"})
        form = _checkout_form(guest)
        guest.post("/checkout/", data=form)
        assert guest.get("/admin/orders/").status_code == 302

    with shop_app.app_context():
        order_id = Order.query.one().id
        assert db.session.get(Product, 1).quantity == 3
    with shop_app.test_client() as regular_user:
        assert _login(regular_user, "staff@example.test").status_code == 302
        assert regular_user.get("/cart/").status_code == 200
        assert regular_user.post("/cart/add/1/", data={
            "csrf_token": _csrf(regular_user.get("/products/")),
            "quantity": "1",
        }).status_code == 302
        assert regular_user.get("/admin/orders/").status_code == 403
        assert regular_user.get("/sales/").location == "/products/"

    with shop_app.test_client() as admin:
        assert _login(admin, "admin@example.test").status_code == 302
        csrf = _csrf(admin.get(f"/admin/orders/{order_id}/"))
        for _ in range(2):
            cancelled = admin.post(f"/admin/orders/{order_id}/", data={
                "csrf_token": csrf,
                "status": "cancelled",
            })
            assert cancelled.status_code == 302
    with shop_app.app_context():
        assert db.session.get(Order, order_id).status == "cancelled"
        assert db.session.get(Product, 1).quantity == 5
        assert Sale.query.count() == 0


def test_products_with_guest_order_history_are_archived_not_deleted(shop_app):
    with shop_app.test_client() as guest:
        csrf = _csrf(guest.get("/products/"))
        guest.post("/cart/add/1/", data={"csrf_token": csrf, "quantity": "1"})
        guest.post("/checkout/", data=_checkout_form(guest))

    with shop_app.test_client() as admin:
        assert _login(admin, "admin@example.test").status_code == 302
        csrf = _csrf(admin.get("/products/"))
        archived = admin.post("/products/1/delete", data={"csrf_token": csrf})
        assert archived.status_code == 302
    with shop_app.app_context():
        product = db.session.get(Product, 1)
        order = Order.query.one()
        assert product.is_active is False
        assert order.items[0].product_id == product.id


def test_deleting_unsold_product_removes_its_persistent_cart_entries(shop_app):
    with shop_app.test_client() as customer:
        assert _login(customer, "staff@example.test").status_code == 302
        csrf = _csrf(customer.get("/products/"))
        customer.post("/cart/add/1/", data={
            "csrf_token": csrf,
            "quantity": "1",
        })

    with shop_app.test_client() as admin:
        assert _login(admin, "admin@example.test").status_code == 302
        csrf = _csrf(admin.get("/products/"))
        response = admin.post("/products/1/delete", data={"csrf_token": csrf})
        assert response.status_code == 302

    with shop_app.app_context():
        assert db.session.get(Product, 1) is None
        assert CartItem.query.count() == 0


def test_shop_css_is_responsive_and_buy_now_has_no_color_selection(shop_app):
    with shop_app.test_client() as client:
        css = client.get("/static/css/shop.css")
        detail = client.get("/products/2/view/")
    stylesheet = css.get_data(as_text=True)
    assert css.status_code == 200
    assert "@media (max-width: 760px)" in stylesheet
    assert "@media (max-width: 520px)" in stylesheet
    assert detail.status_code == 200
    assert b"more colours" not in detail.data.lower()
    assert b"color-swatch" not in detail.data.lower()
