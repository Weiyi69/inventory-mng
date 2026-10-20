import json
import re
from uuid import uuid4
from decimal import Decimal
from io import BytesIO

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
