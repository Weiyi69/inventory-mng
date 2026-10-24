import json
import re
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from flask.testing import FlaskClient

from app import create_app
from config import Config
from models import Product, Sale, SaleItem, User, db


class AuthenticatedTestClient(FlaskClient):
    csrf_token = ""

    def post(self, path, data=None, json=None, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.csrf_token:
            headers.setdefault("X-CSRF-TOKEN", self.csrf_token)
        if path == "/sales/" and json is not None:
            headers.setdefault("Idempotency-Key", uuid4().hex)
        return super().post(path, data=data, json=json, headers=headers, **kwargs)


@pytest.fixture
def client(tmp_path):
    class TestConfig(Config):
        TESTING = True
        APP_ENV = "test"
        SECRET_KEY = "test-secret-key-for-retailflow-authentication"
        JWT_SECRET_KEY = "test-jwt-secret-key-for-retailflow-authentication"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'test.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    app = create_app(TestConfig)
    app.test_client_class = AuthenticatedTestClient
    app.instance_path = str(tmp_path / "instance")
    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = User(name="Test Admin", email="admin@example.test", role="admin")
        admin.set_password("correct-horse-battery")
        db.session.add(admin)
        db.session.add(Product(name="Notebook", sku="NOTE-1", barcode="NOTE-1",
                               category="Stationery", price=Decimal("4.50"),
                               cost=Decimal("2.00"), quantity=10, low_stock_threshold=2))
        db.session.commit()
    with app.test_client() as test_client:
        login_page = test_client.get("/login")
        csrf_token = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data)
        assert csrf_token is not None
        test_client.post("/login", data={
            "csrf_token": csrf_token.group(1).decode(),
            "email": "admin@example.test",
            "password": "correct-horse-battery",
        })
        test_client.csrf_token = test_client.get_cookie("csrf_access_token").value
        yield test_client
    with app.app_context():
        db.drop_all()


def test_sale_decreases_stock_and_records_total(client):
    response = client.post("/sales/", json={
        "items": [{"product_id": 1, "quantity": 2}],
        "amount_tendered": "10.00",
    })

    assert response.status_code == 201
    assert response.json["total"] == "9.00"
    assert response.json["change_due"] == "1.00"
    with client.application.app_context():
        assert Product.query.get(1).quantity == 8
        sale = Sale.query.one()
        assert sale.total == Decimal("9.00")
        assert sale.change_due == Decimal("1.00")
        assert SaleItem.query.one().quantity == 2


def test_sale_rejects_insufficient_stock_without_changing_inventory(client):
    response = client.post("/sales/", json={"items": [{"product_id": 1, "quantity": 11}]})

    assert response.status_code == 400
    with client.application.app_context():
        assert Product.query.get(1).quantity == 10
        assert Sale.query.count() == 0


def test_sale_rejects_empty_cart(client):
    response = client.post("/sales/", json={"items": []})

    assert response.status_code == 400
    assert "at least one product" in response.json["error"]


def test_retried_sale_request_does_not_duplicate_sale_or_stock_deduction(client):
    payload = {"items": [{"product_id": 1, "quantity": 2}]}
    headers = {"Idempotency-Key": "retry-safe-sale-001"}

    first = client.post("/sales/", json=payload, headers=headers)
    retry = client.post("/sales/", json=payload, headers=headers)

    assert first.status_code == 201
    assert retry.status_code == 200
    assert retry.json["duplicate"] is True
    assert retry.json["id"] == first.json["id"]
    with client.application.app_context():
        assert Sale.query.count() == 1
        assert Product.query.get(1).quantity == 8


def test_sale_rejects_insufficient_payment_without_changing_stock(client):
    response = client.post("/sales/", json={
        "items": [{"product_id": 1, "quantity": 2}],
        "amount_tendered": "8.99",
    })

    assert response.status_code == 400
    assert "at least the sale total" in response.json["error"]
    with client.application.app_context():
        assert Product.query.get(1).quantity == 10
        assert Sale.query.count() == 0


def test_invoice_pdf_is_generated(client):
    created = client.post("/sales/", json={"items": [{"product_id": 1, "quantity": 1}]})

    response = client.get(f"/sales/{created.json['id']}/invoice.pdf")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")


def test_product_barcode_is_generated(client):
    client.post("/products/", data={"name": "Pen", "sku": "PEN-1", "price": "1.25",
                                    "quantity": "4"})

    response = client.get("/products/2/barcode.svg")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert b"<svg" in response.data


def test_product_can_be_edited_without_changing_stock_and_replace_photo(client):
    old_filename = "d" * 32 + ".jpg"
    image_directory = (
        Path(client.application.instance_path)
        / "uploads"
        / "products"
    )
    image_directory.mkdir(parents=True, exist_ok=True)
    old_image_path = image_directory / old_filename
    old_image_path.write_bytes(b"old image")
    with client.application.app_context():
        product = db.session.get(Product, 1)
        product.image_filename = old_filename
        db.session.commit()

    original_photo = BytesIO()
    Image.new("RGB", (20, 20), color="green").save(original_photo, format="JPEG")
    edit_page = client.get("/products/1/edit")
    response = client.post(
        "/products/1/edit",
        data={
            "name": "Updated Notebook",
            "sku": "NOTE-UPDATED",
            "barcode": "BAR-UPDATED",
            "category": "Stationery",
            "size": "A5",
            "color": "Green",
            "price": "6.25",
            "cost": "2.50",
            "low_stock_threshold": "4",
            "image": (BytesIO(original_photo.getvalue()), "notebook.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert edit_page.status_code == 200
    assert b'value="Notebook"' in edit_page.data
    assert b'value="10"' not in edit_page.data
    assert response.status_code == 302
    with client.application.app_context():
        product = db.session.get(Product, 1)
        assert product.name == "Updated Notebook"
        assert product.sku == "NOTE-UPDATED"
        assert product.barcode == "BAR-UPDATED"
        assert product.category == "Stationery"
        assert product.size == "A5"
        assert product.color == "Green"
        assert product.price == Decimal("6.25")
        assert product.cost == Decimal("2.50")
        assert product.quantity == 10
        assert product.low_stock_threshold == 4
        new_filename = product.image_filename
        assert new_filename != old_filename
    assert not old_image_path.exists()
    uploaded_image = client.get(f"/products/images/{new_filename}")
    assert uploaded_image.status_code == 200
    assert uploaded_image.data.startswith(b"\xff\xd8")


def test_product_edit_rejects_duplicate_sku_and_preserves_existing_values(client):
    client.post("/products/", data={
        "name": "Pen",
        "sku": "PEN-DUPLICATE",
        "price": "1.00",
        "quantity": "3",
    })

    response = client.post("/products/1/edit", data={
        "name": "Changed Notebook",
        "sku": "PEN-DUPLICATE",
        "barcode": "NOTE-1",
        "category": "Stationery",
        "size": "",
        "color": "",
        "price": "9.99",
        "cost": "3.00",
        "low_stock_threshold": "5",
    })

    assert response.status_code == 302
    assert response.location == "/products/1/edit"
    assert b"SKU and barcode must each be unique" in client.get(response.location).data
    with client.application.app_context():
        product = db.session.get(Product, 1)
        assert product.name == "Notebook"
        assert product.sku == "NOTE-1"
        assert product.price == Decimal("4.50")
        assert product.quantity == 10


def test_product_edit_preserves_cost_for_user_without_cost_permission(client):
    with client.application.app_context():
        user = db.session.get(User, 1)
        user.role = "user"
        user.permissions = {
            "pages": {"products": True},
            "actions": {"products.write": True, "products.costs": False},
        }
        db.session.commit()

    response = client.post("/products/1/edit", data={
        "name": "Notebook",
        "sku": "NOTE-1",
        "barcode": "NOTE-1",
        "category": "Stationery",
        "size": "",
        "color": "",
        "price": "5.00",
        "cost": "0.01",
        "low_stock_threshold": "5",
    })

    assert response.status_code == 302
    with client.application.app_context():
        product = db.session.get(Product, 1)
        assert product.price == Decimal("5.00")
        assert product.cost == Decimal("2.00")


def test_archived_product_must_be_restored_before_editing(client):
    client.post("/sales/", json={"items": [{"product_id": 1, "quantity": 1}]})
    client.post("/products/1/delete")

    edit_page = client.get("/products/1/edit")
    update = client.post("/products/1/edit", data={
        "name": "Changed Notebook",
        "sku": "NOTE-1",
        "barcode": "NOTE-1",
        "category": "Stationery",
        "price": "5.00",
        "low_stock_threshold": "5",
    })

    assert edit_page.status_code == 302
    assert edit_page.location == "/products/?archived=1"
    assert update.status_code == 302
    assert update.location == "/products/?archived=1"
    with client.application.app_context():
        product = db.session.get(Product, 1)
        assert product.name == "Notebook"
        assert product.is_active is False


def test_restock_adds_units_to_existing_inventory(client):
    response = client.post("/products/1/stock", data={"quantity": "6"})

    assert response.status_code == 302
    with client.application.app_context():
        assert Product.query.get(1).quantity == 16


def test_deleting_unsold_product_permanently_removes_it(client):
    response = client.post("/products/1/delete")

    assert response.status_code == 302
    assert b"permanently deleted" in client.get("/products/").data
    with client.application.app_context():
        assert Product.query.get(1) is None


def test_deleting_sold_product_archives_it_and_preserves_sales(client):
    created = client.post("/sales/", json={"items": [{"product_id": 1, "quantity": 1}]})

    response = client.post(f"/products/1/delete")

    assert response.status_code == 302
    assert b"was archived instead of deleted" in client.get("/products/").data
    with client.application.app_context():
        product = db.session.get(Product, 1)
        assert product is not None
        assert product.is_active is False
        assert Sale.query.count() == 1
        assert SaleItem.query.one().product_name == "Notebook"
    assert b"Notebook" not in client.get("/sales/").data
    assert b"Notebook" in client.get("/products/?archived=1").data
    assert client.get(created.json["invoice_url"]).status_code == 200


def test_archived_product_can_be_restored(client):
    client.post("/sales/", json={"items": [{"product_id": 1, "quantity": 1}]})
    client.post("/products/1/delete")

    response = client.post("/products/1/restore")

    assert response.status_code == 302
    with client.application.app_context():
        assert db.session.get(Product, 1).is_active is True
    assert b"Notebook" in client.get("/sales/").data


def test_product_clothing_details_are_saved(client):
    client.post("/products/", data={
        "name": "Classic tee",
        "sku": "TEE-M-BLK",
        "category": "Clothing",
        "size": "M",
        "color": "Black",
        "price": "18.00",
        "cost": "7.50",
        "quantity": "4",
    })

    with client.application.app_context():
        product = Product.query.filter_by(sku="TEE-M-BLK").one()
        assert (product.category, product.size, product.color) == ("Clothing", "M", "Black")


def test_product_photo_upload_is_saved_and_displayed(client):
    image_buffer = BytesIO()
    Image.new("RGB", (32, 24), color="navy").save(image_buffer, format="PNG")
    image_buffer.seek(0)

    client.post("/products/", data={
        "name": "Photo tee",
        "sku": "PHOTO-TEE",
        "price": "18.00",
        "quantity": "4",
        "image": (image_buffer, "shirt.png"),
    }, content_type="multipart/form-data")

    with client.application.app_context():
        product = Product.query.filter_by(sku="PHOTO-TEE").one()
        assert product.image_filename.endswith(".jpg")
        image_url = f"/products/images/{product.image_filename}"
    response = client.get(image_url)
    products_page = client.get("/products/")
    sales_page = client.get("/sales/")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert products_page.status_code == 200
    assert image_url.encode() in products_page.data
    assert image_url.encode() in sales_page.data


def test_product_photo_upload_rejects_non_image_file(client):
    response = client.post("/products/", data={
        "name": "Bad photo",
        "sku": "BAD-PHOTO",
        "price": "18.00",
        "quantity": "4",
        "image": (BytesIO(b"not an image"), "shirt.png"),
    }, content_type="multipart/form-data")

    assert response.status_code == 302
    assert b"not a valid product image" in client.get("/products/").data
    with client.application.app_context():
        assert Product.query.filter_by(sku="BAD-PHOTO").count() == 0


def test_dashboard_and_reports_show_inventory_and_profit_metrics(client):
    dashboard = client.get("/")
    reports = client.get("/reports/")

    assert dashboard.status_code == 200
    assert b"Inventory value" in dashboard.data
    assert b"Estimated profit" in dashboard.data
    assert reports.status_code == 200
    assert b"estimated profit" in reports.data
    assert "₱".encode() in dashboard.data
    assert "₱".encode() in reports.data


def test_dashboard_top_sellers_show_photos_and_keep_initial_fallback(client):
    image_filename = "a" * 32 + ".jpg"
    with client.application.app_context():
        notebook = db.session.get(Product, 1)
        notebook.image_filename = image_filename
        duplicate_notebook = Product(
            name="Notebook",
            sku="NOTE-2",
            barcode="NOTE-2",
            category="Stationery",
            price=Decimal("4.50"),
            cost=Decimal("2.00"),
            quantity=10,
            low_stock_threshold=2,
        )
        pen = Product(
            name="Pen",
            sku="PEN-1",
            barcode="PEN-1",
            category="Stationery",
            price=Decimal("1.00"),
            cost=Decimal("0.50"),
            quantity=10,
            low_stock_threshold=2,
        )
        db.session.add_all([duplicate_notebook, pen])
        db.session.flush()
        duplicate_notebook_id = duplicate_notebook.id
        pen_id = pen.id

        image_path = (
            Path(client.application.instance_path)
            / "uploads"
            / "products"
            / image_filename
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"product image")
        db.session.commit()

    sale = client.post("/sales/", json={
        "items": [
            {"product_id": 1, "quantity": 1},
            {"product_id": duplicate_notebook_id, "quantity": 2},
            {"product_id": pen_id, "quantity": 1},
        ],
    })
    dashboard = client.get("/")
    image = client.get(f"/products/images/{image_filename}")

    assert sale.status_code == 201
    assert dashboard.status_code == 200
    assert f"/products/images/{image_filename}".encode() in dashboard.data
    assert b"3 units" in dashboard.data
    assert b"1 units" in dashboard.data
    assert b'class="product-mark">P</div>' in dashboard.data
    assert image.status_code == 200
    assert image.data == b"product image"


def test_recent_sale_rows_show_first_item_photo_or_sale_badge_fallback(client):
    image_filename = "c" * 32 + ".jpg"
    with client.application.app_context():
        notebook = db.session.get(Product, 1)
        notebook.image_filename = image_filename
        tee = Product(
            name="Daily Tee",
            sku="TEE-RECENT",
            barcode="TEE-RECENT",
            category="Clothing",
            price=Decimal("10.00"),
            cost=Decimal("5.00"),
            quantity=10,
            low_stock_threshold=2,
        )
        db.session.add(tee)
        db.session.flush()
        tee_id = tee.id

        image_path = (
            Path(client.application.instance_path)
            / "uploads"
            / "products"
            / image_filename
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"product image")
        db.session.commit()

    image_sale = client.post("/sales/", json={
        "items": [{"product_id": 1, "quantity": 1}],
    })
    no_image_sale = client.post("/sales/", json={
        "items": [{"product_id": tee_id, "quantity": 1}],
    })
    sales_page = client.get("/sales/")
    dashboard = client.get("/")

    assert image_sale.status_code == 201
    assert no_image_sale.status_code == 201
    for response in (sales_page, dashboard):
        assert response.status_code == 200
        assert f"/products/images/{image_filename}".encode() in response.data
        assert (
            f'class="sale-mark">#{no_image_sale.json["id"]}</div>'.encode()
            in response.data
        )
        assert (
            f'class="sale-mark">#{image_sale.json["id"]}</div>'.encode()
            not in response.data
        )


def test_currency_is_peso_in_product_pos_and_expense_pages(client):
    client.post("/products/", data={
        "name": "Pen",
        "sku": "PESO-PEN",
        "price": "12.50",
        "quantity": "4",
    })
    client.post("/expenses/", data={
        "description": "Delivery",
        "amount": "20.00",
    })

    assert "₱12.50".encode() in client.get("/products/").data
    assert "Cash received (₱)".encode() in client.get("/sales/").data
    assert "₱20.00".encode() in client.get("/expenses/").data


def test_pos_product_data_is_defined_before_shared_script(client):
    page = client.get("/sales/")

    assert page.status_code == 200
    product_data_index = page.data.index(b"window.retailFlowProductData = [")
    app_script_index = page.data.index(b"/static/js/app.js")
    assert product_data_index < app_script_index
    assert b'name: "Notebook"' in page.data


@pytest.mark.parametrize(
    ("range_key", "expected_buckets"),
    [("24h", 24), ("7d", 7), ("4w", 4)],
)
def test_reports_support_selected_time_ranges(client, range_key, expected_buckets):
    response = client.get(f"/reports/?range={range_key}")
    match = re.search(rb"const labels = (\[[^\n]*\]);", response.data)

    assert response.status_code == 200
    assert match is not None
    assert len(json.loads(match.group(1))) == expected_buckets
    assert f'value="{range_key}" selected'.encode() in response.data


def test_all_time_report_groups_activity_by_month(client):
    response = client.get("/reports/?range=all")
    match = re.search(rb"const labels = (\[[^\n]*\]);", response.data)

    assert response.status_code == 200
    assert match is not None
    labels = json.loads(match.group(1))
    assert labels
    assert len(labels) >= 1
    assert f'value="all" selected'.encode() in response.data


def test_report_product_performance_shows_photos_and_initial_fallback(
    client,
    monkeypatch,
):
    image_filename = "b" * 32 + ".jpg"
    with client.application.app_context():
        notebook = db.session.get(Product, 1)
        notebook.image_filename = image_filename
        pen = Product(
            name="Pen",
            sku="PEN-REPORT",
            barcode="PEN-REPORT",
            category="Stationery",
            price=Decimal("1.00"),
            cost=Decimal("0.50"),
            quantity=10,
            low_stock_threshold=2,
        )
        db.session.add(pen)
        db.session.flush()
        pen_id = pen.id

        image_path = (
            Path(client.application.instance_path)
            / "uploads"
            / "products"
            / image_filename
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"product image")
        db.session.commit()

    sale = client.post("/sales/", json={
        "items": [
            {"product_id": 1, "quantity": 1},
            {"product_id": pen_id, "quantity": 2},
        ],
    })
    financial_report = client.get("/reports/")

    assert sale.status_code == 201
    assert financial_report.status_code == 200
    assert f"/products/images/{image_filename}".encode() in financial_report.data
    assert b'class="product-mark">P</span>' in financial_report.data
    assert "₱4.50".encode() in financial_report.data

    with client.application.app_context():
        user = db.session.get(User, 1)
        user.role = "user"
        db.session.commit()

    monkeypatch.setattr(
        User,
        "allows",
        lambda self, permission: permission in {"reports", "products"},
    )
    limited_report = client.get("/reports/")

    assert limited_report.status_code == 200
    assert f"/products/images/{image_filename}".encode() in limited_report.data
    assert b'class="product-mark">P</span>' in limited_report.data
    assert b"TOTAL REVENUE" not in limited_report.data


def test_reports_reject_unknown_time_range(client):
    response = client.get("/reports/?range=unknown")

    assert response.status_code == 400


def test_receipt_includes_clothing_variant_and_change(client):
    with client.application.app_context():
        product = Product.query.get(1)
        product.size = "M"
        product.color = "Navy"
        db.session.commit()
    created = client.post("/sales/", json={
        "items": [{"product_id": 1, "quantity": 1}],
        "amount_tendered": "5.00",
    })

    receipt = client.get(created.json["invoice_url"])

    assert receipt.status_code == 200
    assert b"Navy" in receipt.data
    assert b"Cash received" in receipt.data
    assert "₱0.50".encode() in receipt.data
