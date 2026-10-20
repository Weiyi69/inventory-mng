import re
import time
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from flask.testing import FlaskClient
from flask_jwt_extended import decode_token

from app import create_app
from config import Config
from models import AuditLog, Product, RevokedToken, User, db


class AuthenticatedTestClient(FlaskClient):
    csrf_token = ""

    def post(self, path, data=None, json=None, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.csrf_token:
            headers.setdefault("X-CSRF-TOKEN", self.csrf_token)
        return super().post(path, data=data, json=json, headers=headers, **kwargs)


@pytest.fixture
def auth_app(tmp_path):
    class TestConfig(Config):
        TESTING = True
        SECRET_KEY = "test-secret-key-for-retailflow-authentication"
        JWT_SECRET_KEY = "test-jwt-secret-key-for-retailflow-authentication"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'auth-test.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    app = create_app(TestConfig)
    app.test_client_class = AuthenticatedTestClient
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = User(name="Admin", email="admin@example.test", role="admin")
        admin.set_password("correct-horse-battery")
        user = User(name="Staff", email="staff@example.test", role="user")
        user.set_password("correct-horse-battery")
        product = Product(
            name="Notebook",
            sku="NOTE-1",
            barcode="NOTE-1",
            category="Stationery",
            price=10,
            cost=4,
            quantity=8,
            low_stock_threshold=2,
        )
        db.session.add_all([admin, user, product])
        db.session.commit()
    yield app
    with app.app_context():
        db.drop_all()


def login(client, email="admin@example.test", password="correct-horse-battery"):
    page = client.get("/login")
    token_match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token_match
    response = client.post("/login", data={
        "csrf_token": token_match.group(1).decode(),
        "email": email,
        "password": password,
    })
    if response.status_code == 302:
        client.csrf_token = client.get_cookie("csrf_access_token").value
    return response


@pytest.fixture
def client(auth_app):
    with auth_app.test_client() as test_client:
        client = test_client
        assert login(client).status_code == 302
        yield client


def test_login_logout_and_audit_logging(auth_app):
    with auth_app.test_client() as test_client:
        client = test_client
        response = login(client)
        assert response.status_code == 302
        assert response.location == "/"
        assert client.get("/profile").status_code == 200

        logout = client.post("/auth/logout")

        assert logout.status_code == 302
        public_catalog = client.get("/products/")
        assert public_catalog.status_code == 200
        assert b"Log out" not in public_catalog.data
    with auth_app.app_context():
        events = {event.event for event in AuditLog.query.all()}
        assert {"login_succeeded", "logout"} <= events
        assert RevokedToken.query.filter_by(token_type="refresh").count() == 1


def test_login_rejects_invalid_credentials_and_inactive_accounts(auth_app):
    with auth_app.app_context():
        user = User.query.filter_by(email="staff@example.test").one()
        user.is_active = False
        db.session.commit()

    with auth_app.test_client() as raw_client:
        test_client = raw_client
        response = login(test_client, email="staff@example.test")
        assert response.status_code == 401
        assert b"Email or password is incorrect." in response.data
        assert test_client.get("/products/").status_code == 200


def test_cookie_authenticated_logout_requires_csrf(auth_app):
    with auth_app.test_client() as raw_client:
        test_client = raw_client
        assert login(test_client).status_code == 302
        test_client.csrf_token = ""

        response = test_client.post("/auth/logout")

        assert response.status_code == 401


def test_expired_access_token_allows_public_catalog_but_not_protected_pages(auth_app):
    auth_app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(seconds=1)
    with auth_app.test_client() as raw_client:
        test_client = raw_client
        assert login(test_client).status_code == 302
        time.sleep(1.1)
        access_cookie = test_client.get_cookie("access_token_cookie")
        assert access_cookie
        claims = decode_token(access_cookie.value, allow_expired=True)
        assert claims["exp"] <= time.time()

        response = test_client.get("/products/")

        assert response.status_code == 200
        assert b"Shop products" in response.data
        assert test_client.get_cookie("access_token_cookie") is None
        public_home = test_client.get("/")
        assert public_home.status_code == 302
        assert public_home.location == "/products/"
        restricted = test_client.get("/sales/")
        assert restricted.status_code == 302
        assert restricted.location.startswith("/login")


def test_anonymous_home_redirects_to_public_product_catalog(auth_app):
    with auth_app.test_client() as test_client:
        response = test_client.get("/")
        assert response.status_code == 302
        assert response.location == "/products/"
        catalog = test_client.get(response.location)
        assert catalog.status_code == 200
        assert b"Shop products" in catalog.data


def test_refresh_rotates_and_revokes_previous_token(auth_app):
    with auth_app.test_client() as raw_client:
        test_client = raw_client
        assert login(test_client).status_code == 302
        previous_refresh = test_client.get_cookie(
            "refresh_token_cookie",
            path="/auth",
        ).value
        previous_csrf = test_client.get_cookie("csrf_refresh_token").value

        refreshed = test_client.post(
            "/auth/refresh",
            headers={"X-CSRF-TOKEN": previous_csrf},
        )

        assert refreshed.status_code == 200
        assert refreshed.json["csrf_token"]
        with auth_app.app_context():
            assert RevokedToken.query.filter_by(token_type="refresh").count() == 1
        rotated_refresh = test_client.get_cookie(
            "refresh_token_cookie",
            path="/auth",
        ).value
        assert rotated_refresh != previous_refresh

        test_client.set_cookie(
            "refresh_token_cookie",
            previous_refresh,
            path="/auth",
        )
        test_client.set_cookie(
            "csrf_refresh_token",
            previous_csrf,
            path="/",
        )
        reused = test_client.post(
            "/auth/refresh",
            headers={"X-CSRF-TOKEN": previous_csrf},
        )
        assert reused.status_code == 401


def test_standard_user_cannot_access_admin_or_product_mutations(auth_app):
    with auth_app.test_client() as raw_client:
        test_client = raw_client
        assert login(test_client, email="staff@example.test").status_code == 302

        products_page = test_client.get("/products/")
        assert products_page.status_code == 200
        assert b"<th>COST</th>" not in products_page.data
        assert test_client.get("/admin/users").status_code == 403
        assert test_client.post("/products/1/delete").status_code == 403
        assert test_client.post("/products/1/stock", data={"quantity": "5"}).status_code == 403
        assert test_client.post(
            "/products/",
            data={"name": "Unauthorized", "sku": "UNAUTHORIZED", "price": "1.00"},
        ).status_code == 403
        assert test_client.post(
            "/products/1/restore",
        ).status_code == 403
        for path in ("/", "/sales/", "/expenses/", "/reports/"):
            response = test_client.get(path)
            assert response.status_code == 302
            assert response.location == "/products/"
        assert test_client.get("/profile").location == "/products/"
        for label in (b"Overview", b"Sales", b"Expenses", b"Reports"):
            assert label not in products_page.data
        assert b"Products" in products_page.data
        sale = test_client.post(
            "/sales/",
            json={"items": [{"product_id": 1, "quantity": 1}]},
            headers={"Idempotency-Key": "staff-sale-001"},
        )
        assert sale.status_code == 302
        assert test_client.post(
            "/expenses/",
            data={"description": "Delivery", "amount": "10.00"},
        ).status_code == 302
    with auth_app.app_context():
        assert Product.query.get(1).quantity == 8


def test_admin_can_access_admin_user_management(client):
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/audit").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/sales/").status_code == 200
    assert client.get("/expenses/").status_code == 200
    assert client.get("/reports/").status_code == 200


def test_standard_user_catalog_search_category_and_image_display(auth_app):
    image_bytes = BytesIO()
    Image.new("RGB", (24, 32), color="navy").save(image_bytes, format="JPEG")
    image_path = Path(auth_app.instance_path) / "uploads" / "products"
    image_path.mkdir(parents=True, exist_ok=True)
    filename = "a" * 32 + ".jpg"
    (image_path / filename).write_bytes(image_bytes.getvalue())
    with auth_app.app_context():
        db.session.add(Product(
            name="Blue Hoodie",
            sku="HOODIE-BLUE",
            barcode="1000001",
            category="Hoodies",
            price=Decimal("1500.00"),
            cost=Decimal("700.00"),
            quantity=4,
            low_stock_threshold=1,
            image_filename=filename,
        ))
        db.session.commit()

    with auth_app.test_client() as raw_client:
        user_client = raw_client
        assert login(user_client, email="staff@example.test").status_code == 302
        assert user_client.get_cookie("access_token_cookie")
        catalog = user_client.get("/products/")
        filtered = user_client.get("/products/?q=hoodie&category=Hoodies")
        image = user_client.get(f"/products/images/{filename}")

    assert b"Blue Hoodie" in catalog.data
    assert "₱1,500.00".encode() in catalog.data
    assert b"New Arrivals" in catalog.data
    assert b"object-fit:contain" not in catalog.data
    assert f"/products/images/{filename}".encode() in catalog.data
    assert b"<th>COST</th>" not in catalog.data
    assert b"700.00" not in catalog.data
    assert b"Photo coming soon" in catalog.data
    assert b"Blue Hoodie" in filtered.data
    assert b"Notebook" not in filtered.data
    assert image.status_code == 200
    assert image.mimetype == "image/jpeg"


def test_regular_user_product_detail_is_read_only_and_uses_supported_fields(auth_app):
    with auth_app.app_context():
        product = db.session.get(Product, 1)
        product.size = "M"
        product.color = "Navy"
        db.session.commit()

    with auth_app.test_client() as raw_client:
        user_client = raw_client
        assert login(user_client, email="staff@example.test").status_code == 302
        catalog = user_client.get("/products/")
        detail = user_client.get("/products/1/view/")
        missing = user_client.get("/products/999/view/")
        with auth_app.app_context():
            product = db.session.get(Product, 1)
            product.is_active = False
            db.session.commit()
        inactive = user_client.get("/products/1/view/")

    assert b"/products/1/view/" in catalog.data
    assert detail.status_code == 200
    assert b"Notebook" in detail.data
    assert "₱10.00".encode() in detail.data
    assert b"NOTE-1" in detail.data
    assert b"Stationery" in detail.data
    assert b"Size" in detail.data and b">M<" in detail.data
    assert b"Navy" in detail.data
    assert b"Product image coming soon" in detail.data
    assert b"Stock Information" not in detail.data
    assert b"available" not in detail.data.lower()
    assert "₱4.00".encode() not in detail.data
    assert b"Product Details" in detail.data
    assert b"<details" in detail.data and b"<summary" in detail.data
    assert b"Size Guide" not in detail.data
    assert b"Description" not in detail.data
    assert b"color-swatch" not in detail.data.lower()
    assert b"more colours" not in detail.data.lower()
    assert b"add to cart" not in detail.data.lower()
    assert b"buy it now" not in detail.data.lower()
    assert b'name="quantity"' not in detail.data
    assert missing.status_code == 404
    assert inactive.status_code == 404


def test_detail_page_displays_real_photo_and_authorized_stock_only(auth_app):
    image_bytes = BytesIO()
    Image.new("RGB", (32, 40), color="black").save(image_bytes, format="JPEG")
    image_path = Path(auth_app.instance_path) / "uploads" / "products"
    image_path.mkdir(parents=True, exist_ok=True)
    filename = "b" * 32 + ".jpg"
    (image_path / filename).write_bytes(image_bytes.getvalue())
    with auth_app.app_context():
        product = db.session.get(Product, 1)
        product.image_filename = filename
        db.session.commit()

    with auth_app.test_client() as user_client:
        assert login(user_client, email="staff@example.test").status_code == 302
        regular_detail = user_client.get("/products/1/view/")
        with auth_app.app_context():
            user = User.query.filter_by(email="staff@example.test").one()
            permissions = {
                section: values.copy()
                for section, values in user.permissions.items()
            }
            permissions["actions"]["products.stock_view"] = True
            user.permissions = permissions
            db.session.commit()
        granted_detail = user_client.get("/products/1/view/")
        photo = user_client.get(f"/products/images/{filename}")

    assert f"/products/images/{filename}".encode() in regular_detail.data
    assert b"Stock Information" not in regular_detail.data
    assert b"8 available" not in regular_detail.data
    assert b"Stock Information" in granted_detail.data
    assert b"8 available" in granted_detail.data
    assert photo.status_code == 200
    assert photo.mimetype == "image/jpeg"


def test_unavailable_product_photo_uses_detail_placeholder(auth_app):
    with auth_app.app_context():
        product = db.session.get(Product, 1)
        product.image_filename = "c" * 32 + ".jpg"
        db.session.commit()

    with auth_app.test_client() as raw_client:
        assert login(raw_client, email="staff@example.test").status_code == 302
        detail = raw_client.get("/products/1/view/")
        image = raw_client.get(f"/products/images/{'c' * 32}.jpg")

    assert detail.status_code == 200
    assert image.status_code == 404
    assert b"Product image unavailable" in detail.data
    assert b'detailImage.addEventListener("error"' in detail.data


def test_admin_can_view_product_detail_and_stock_but_catalog_table_is_unchanged(client):
    admin_detail = client.get("/products/1/view/")
    admin_products = client.get("/products/")

    assert admin_detail.status_code == 200
    assert b"Stock Information" in admin_detail.data
    assert b"8 available" in admin_detail.data
    assert b"<th>COST</th>" in admin_products.data
    assert b"RESTOCK" in admin_products.data
    assert b"barcode-link" in admin_products.data


def test_detail_css_is_responsive_and_accessible(auth_app):
    with auth_app.test_client() as raw_client:
        assert login(raw_client, email="staff@example.test").status_code == 302
        response = raw_client.get("/static/css/product-detail.css")

    assert response.status_code == 200
    stylesheet = response.get_data(as_text=True)
    assert "grid-template-columns: minmax(0, 1.08fr)" in stylesheet
    assert "@media (max-width: 760px)" in stylesheet
    assert "@media (max-width: 480px)" in stylesheet
    assert "object-fit: contain" in stylesheet
    assert "summary:focus-visible" in stylesheet


def test_catalog_styles_provide_responsive_columns_and_contained_images(auth_app):
    with auth_app.test_client() as raw_client:
        assert login(raw_client, email="staff@example.test").status_code == 302
        response = raw_client.get("/static/css/catalog.css")

    assert response.status_code == 200
    stylesheet = response.get_data(as_text=True)
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in stylesheet
    assert "@media (max-width: 960px)" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in stylesheet
    assert "@media (max-width: 360px)" in stylesheet
    assert "grid-template-columns: 1fr" in stylesheet
    assert "object-fit: contain" in stylesheet


def test_create_admin_command_refuses_a_second_bootstrap(auth_app):
    result = auth_app.test_cli_runner().invoke(args=["create-admin"])

    assert result.exit_code != 0
    assert "An administrator already exists" in result.output


def test_create_admin_command_succeeds_without_http_request(auth_app):
    with auth_app.app_context():
        User.query.filter_by(role="admin").delete()
        db.session.commit()

    result = auth_app.test_cli_runner().invoke(
        args=["create-admin"],
        input=(
            "First Admin\n"
            "first-admin@example.test\n"
            "correct-horse-battery\n"
            "correct-horse-battery\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Administrator account created" in result.output
    with auth_app.app_context():
        admin = User.query.filter_by(email="first-admin@example.test").one()
        assert admin.role == "admin"
        event = AuditLog.query.filter_by(event="admin_bootstrapped").one()
        assert event.target_user_id == admin.id
        assert event.ip_address is None


def test_admin_can_create_user_with_configurable_permissions(client, auth_app):
    response = client.post("/admin/users", data={
        "name": "Inventory Clerk",
        "email": "clerk@example.test",
        "password": "correct-horse-battery",
        "role": "user",
        "page_products": "on",
        "page_sales": "on",
        "action_products_costs": "on",
        "action_products_write": "on",
        "action_products_stock_view": "on",
        "action_sales_process": "on",
    })
    assert response.status_code == 302

    with auth_app.app_context():
        clerk = User.query.filter_by(email="clerk@example.test").one()
        assert clerk.role == "user"
        assert clerk.permissions["pages"]["products"] is True
        assert clerk.permissions["actions"]["products.costs"] is True
        assert clerk.permissions["actions"]["products.write"] is True
        assert clerk.permissions["actions"]["products.stock_view"] is True
        assert clerk.permissions["pages"]["sales"] is False
        assert clerk.permissions["actions"]["sales.process"] is False
        assert AuditLog.query.filter_by(event="user_created").count() == 1
    with auth_app.test_client() as clerk_client:
        response = login(clerk_client, email="clerk@example.test")
        assert response.status_code == 302
        assert response.location == "/products/"
        catalog = clerk_client.get("/products/").data
        assert b"<th>COST</th>" not in catalog
        assert b"Blue Hoodie" not in catalog
        created = clerk_client.post("/products/", data={
            "name": "Granted Product",
            "sku": "GRANTED-PRODUCT",
            "price": "12.00",
            "cost": "4.00",
            "quantity": "3",
        })
        assert created.status_code == 302
    with auth_app.app_context():
        assert Product.query.filter_by(sku="GRANTED-PRODUCT").count() == 1


def test_deactivated_account_loses_existing_session(auth_app):
    with auth_app.test_client() as raw_user_client:
        user_client = raw_user_client
        assert login(user_client, email="staff@example.test").status_code == 302
        with auth_app.app_context():
            user = User.query.filter_by(email="staff@example.test").one()
            user.is_active = False
            user.auth_version += 1
            db.session.commit()
        assert user_client.get("/products/").status_code == 302


def test_user_cannot_create_or_promote_accounts(auth_app):
    with auth_app.test_client() as raw_user_client:
        user_client = raw_user_client
        assert login(user_client, email="staff@example.test").status_code == 302
        response = user_client.post("/admin/users", data={
            "name": "Promoted",
            "email": "promoted@example.test",
            "password": "correct-horse-battery",
            "role": "admin",
        })
        assert response.status_code == 403
    with auth_app.app_context():
        assert User.query.filter_by(email="promoted@example.test").count() == 0


def test_unprotected_sale_api_rejects_requests_without_login(auth_app):
    with auth_app.test_client() as test_client:
        response = test_client.post(
            "/sales/",
            json={"items": [{"product_id": 1, "quantity": 1}]},
        )
        assert response.status_code == 401


def test_app_rejects_missing_or_weak_production_secrets():
    class WeakConfig(Config):
        TESTING = False
        SECRET_KEY = "not-a-secure-secret"
        JWT_SECRET_KEY = "not-a-secure-jwt-secret"

    with pytest.raises(RuntimeError, match="distinct SECRET_KEY and JWT_SECRET_KEY"):
        create_app(WeakConfig)
