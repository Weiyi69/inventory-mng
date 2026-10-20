from sqlalchemy import text

from app import create_app
from config import Config
from models import Expense, Order, Product, Sale, SaleItem, db


def test_auth_migration_preserves_existing_business_records(tmp_path):
    class MigrationTestConfig(Config):
        TESTING = True
        SECRET_KEY = "migration-test-secret-for-retailflow"
        JWT_SECRET_KEY = "migration-test-jwt-secret-for-retailflow"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'migration-test.db'}"

    app = create_app(MigrationTestConfig)
    runner = app.test_cli_runner()
    baseline = runner.invoke(args=["db", "upgrade", "0001_legacy"])
    assert baseline.exit_code == 0, baseline.output

    with app.app_context():
        db.session.execute(text("""
            INSERT INTO product (
                name, sku, barcode, category, price, cost, quantity,
                low_stock_threshold, created_at, size, color, image_filename,
                is_active
            ) VALUES (
                'Notebook', 'NOTE-1', 'BAR-1', 'Stationery', 10.00, 4.00, 7,
                2, '2025-01-02 10:00:00', NULL, NULL, NULL, 1
            )
        """))
        db.session.execute(text("""
            INSERT INTO sale (created_at, total, amount_tendered)
            VALUES ('2025-01-02 10:30:00', 10.00, 10.00)
        """))
        db.session.execute(text("""
            INSERT INTO sale_item (
                sale_id, product_id, product_name, quantity, unit_price,
                product_size, product_color, unit_cost
            ) VALUES (1, 1, 'Notebook', 1, 10.00, NULL, NULL, 4.00)
        """))
        db.session.execute(text("""
            INSERT INTO expense (description, category, amount, created_at)
            VALUES ('Delivery', 'Shipping', 2.00, '2025-01-02 11:00:00')
        """))
        db.session.commit()
        db.session.execute(text("DROP TABLE alembic_version"))
        db.session.commit()

    stamped = runner.invoke(args=["db", "stamp", "0001_legacy"])
    assert stamped.exit_code == 0, stamped.output
    upgraded = runner.invoke(args=["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    checked = runner.invoke(args=["db", "check"])
    assert checked.exit_code == 0, checked.output

    with app.app_context():
        product = Product.query.filter_by(sku="NOTE-1").one()
        sale = Sale.query.one()
        item = SaleItem.query.one()
        expense = Expense.query.one()
        assert (product.barcode, product.quantity) == ("BAR-1", 7)
        assert (sale.status, sale.total, sale.created_by_id) == (
            "completed",
            10,
            None,
        )
        assert (item.product_name, item.unit_cost) == ("Notebook", 4)
        assert (expense.description, expense.amount, expense.created_by_id) == (
            "Delivery",
            2,
            None,
        )
        assert sale.order_id is None
        assert Order.query.count() == 0
