"""Tests for the paths that break first: auth, stock maths, orders, batches,
translations, notifications and the API.

    python -m pytest tests -q
"""

import os
import pathlib
import re
import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from inventory import create_app
from inventory.extensions import db
from inventory.models import (
    ORDER_COMPLETE,
    Batch,
    LoginAttempt,
    Product,
    StockMovement,
    Supplier,
    User,
    Warehouse,
)
from inventory.notifier import check_stock_levels
from inventory.seed import run_seed
from inventory.utils import barcode_variants
from inventory.services import (
    StockError,
    adjust_stock,
    allocate_batches,
    batch_summary,
    issue_stock,
    receive_stock,
    reorder_recommendation,
    set_setting,
    transfer_stock,
)


@pytest.fixture()
def app():
    application = create_app("testing")
    application.config["ALLOW_PRIVATE_WEBHOOKS"] = True
    with application.app_context():
        db.create_all()
        run_seed(demo=True)
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def warehouse(app):
    with app.app_context():
        return db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalars().one()


@pytest.fixture()
def client(app):
    return app.test_client()


def sign_in(application, username="admin", password="Admin@12345"):
    client = application.test_client()

    def token():
        body = client.get("/auth/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    client.post("/auth/login", data={"identity": username, "password": password,
                                     "csrf_token": token()})
    return client


@pytest.fixture()
def login(client):
    def _login(username="admin", password="Admin@12345"):
        return client.post(
            "/auth/login",
            data={"identity": username, "password": password},
            follow_redirects=True,
        )

    return _login


def _make_product(**overrides):
    warehouse = db.session.execute(
        db.select(Warehouse).where(Warehouse.code == "WH-01")
    ).scalars().one()
    data = {
        "sku": "TEST-001",
        "name": "Test widget",
        "cost": 5,
        "price": 12.5,
        "reorder_point": 10,
        "reorder_quantity": 25,
    }
    data.update(overrides)
    product = Product(**data)
    db.session.add(product)
    db.session.flush()
    return product, warehouse


def test_health_endpoint(app):
    with app.test_client() as c:
        assert c.get("/api/health").get_json()["ok"] is True


def test_root_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_login_and_logout(client, login):
    body = login().get_data(as_text=True)
    assert "Welcome back" in body
    assert client.get("/products").status_code == 200

    client.post("/auth/logout", follow_redirects=True)
    assert client.get("/products").status_code == 302


def test_bad_password_is_rejected(client):
    response = client.post("/auth/login", data={"identity": "admin", "password": "wrong"})
    assert "do not match" in response.get_data(as_text=True)


def test_receive_updates_balance_and_ledger(app):
    with app.app_context():
        product, warehouse = _make_product()
        receive_stock(product, warehouse, 40, unit_cost=5)
        db.session.commit()

        assert product.quantity == 40
        movement = db.session.execute(
            db.select(StockMovement).where(StockMovement.product_id == product.id)
        ).scalars().one()
        assert movement.quantity == 40
        assert movement.balance_after == 40
        assert movement.move_type == "receipt"


def test_issue_blocks_going_negative(app):
    with app.app_context():
        product, warehouse = _make_product()
        receive_stock(product, warehouse, 5)
        db.session.commit()

        with pytest.raises(StockError):
            issue_stock(product, warehouse, 9)
        db.session.rollback()


def test_transfer_writes_paired_entries(app):
    with app.app_context():
        product, source = _make_product()
        destination = Warehouse(code="WH-DEST", name="Destination site")
        db.session.add(destination)
        db.session.flush()

        receive_stock(product, source, 20)
        transfer_stock(product, source, destination, 8)
        db.session.commit()

        assert product.stock_at(source.id) == 12
        assert product.stock_at(destination.id) == 8

        movements = db.session.execute(
            db.select(StockMovement)
            .where(StockMovement.product_id == product.id)
            .order_by(StockMovement.id)
        ).scalars().all()
        assert [m.move_type for m in movements[-2:]] == ["transfer_out", "transfer_in"]
        assert movements[-1].reference == movements[-2].reference


def test_adjustment_records_the_difference(app):
    with app.app_context():
        product, warehouse = _make_product()
        receive_stock(product, warehouse, 30)
        _, delta = adjust_stock(product, warehouse, 27, note="Damaged in transit")
        db.session.commit()

        assert delta == -3
        assert product.quantity == 27
        adjustment = db.session.execute(
            db.select(StockMovement).where(StockMovement.move_type == "adjustment")
        ).scalars().one()
        assert adjustment.quantity == -3
        assert "Damaged" in adjustment.note


def test_moving_average_cost_blends_incoming_price(app):
    with app.app_context():
        product, warehouse = _make_product(cost=10, price=20)
        receive_stock(product, warehouse, 10, unit_cost=10, track_cost=False)
        receive_stock(product, warehouse, 30, unit_cost=20)
        db.session.commit()

        # (10 x 10 + 30 x 20) / 40 = 17.50
        assert float(product.cost) == 17.50


def test_purchase_order_receive_updates_stock(app, client, login):
    with app.app_context():
        product, warehouse = _make_product()
        supplier = Supplier(name="Test Supplier")
        db.session.add(supplier)
        db.session.commit()
        product_id, warehouse_id, supplier_id = product.id, warehouse.id, supplier.id

    login()

    created = client.post(
        "/purchase-orders/new",
        data={
            "supplier_id": supplier_id,
            "warehouse_id": warehouse_id,
            "product_id": [str(product_id)],
            "quantity": ["25"],
            "unit_cost": ["4.50"],
        },
        follow_redirects=True,
    )
    assert "Purchase order PO-" in created.get_data(as_text=True)

    with app.app_context():
        from inventory.models import PurchaseOrder

        order = db.session.execute(db.select(PurchaseOrder)).scalars().one()
        assert order.status == "draft"
        assert order.total_cost == 112.50
        order_id = order.id

    received = client.post(f"/purchase-orders/{order_id}/receive", data={}, follow_redirects=True)
    assert "received into" in received.get_data(as_text=True)

    with app.app_context():
        refreshed = db.session.get(PurchaseOrder, order_id)
        product = db.session.get(Product, product_id)
        assert refreshed.status == ORDER_COMPLETE
        assert product.quantity == 25
        # average cost should now sit at the purchase price
        assert float(product.cost) == 4.50


def test_sales_order_fulfilment_deducts_stock(app, client, login):
    with app.app_context():
        product, warehouse = _make_product()
        receive_stock(product, warehouse, 12)
        db.session.commit()
        product_id, warehouse_id = product.id, warehouse.id

    login()
    response = client.post(
        "/sales-orders/new",
        data={
            "customer_name": "Test Customer",
            "warehouse_id": warehouse_id,
            "product_id": [str(product_id)],
            "quantity": ["5"],
            "unit_price": ["12.50"],
        },
        follow_redirects=True,
    )
    assert "Sales order SO-" in response.get_data(as_text=True)

    with app.app_context():
        from inventory.models import SalesOrder

        order = db.session.execute(db.select(SalesOrder)).scalars().one()
        assert order.total == 62.50
        order_id = order.id

    client.post(f"/sales-orders/{order_id}/fulfil", data={}, follow_redirects=True)

    with app.app_context():
        assert db.session.get(SalesOrder, order_id).status == ORDER_COMPLETE
        assert db.session.get(Product, product_id).quantity == 7


def test_sales_order_blocked_without_stock(app, client, login):
    with app.app_context():
        product, warehouse = _make_product()
        db.session.commit()
        product_id, warehouse_id = product.id, warehouse.id

    login()
    client.post(
        "/sales-orders/new",
        data={
            "customer_name": "No Stock Ltd",
            "warehouse_id": warehouse_id,
            "product_id": [str(product_id)],
            "quantity": ["3"],
            "unit_price": ["12.50"],
        },
        follow_redirects=True,
    )
    with app.app_context():
        from inventory.models import SalesOrder

        order = db.session.execute(db.select(SalesOrder)).scalars().one()
        order_id = order.id

    response = client.post(f"/sales-orders/{order_id}/fulfil", data={}, follow_redirects=True)
    assert "available in" in response.get_data(as_text=True)

    with app.app_context():
        assert db.session.get(SalesOrder, order_id).status == "draft"
        assert db.session.get(Product, product_id).quantity == 0


def test_every_main_page_renders(client, login):
    login()
    for path in [
        "/",
        "/products",
        "/products/new",
        "/catalog/categories",
        "/catalog/suppliers",
        "/catalog/suppliers/new",
        "/warehouses",
        "/warehouses/new",
        "/warehouses/stock",
        "/stock/movements",
        "/stock/receive",
        "/stock/issue",
        "/stock/adjust",
        "/stock/transfer",
        "/purchase-orders",
        "/purchase-orders/new",
        "/sales-orders",
        "/sales-orders/new",
        "/reports",
        "/reports/valuation",
        "/reports/low-stock",
        "/reports/abc",
        "/reports/ageing",
        "/reports/dead-stock",
        "/reports/suppliers",
        "/reports/audit",
        "/scan",
        "/users",
        "/settings",
        "/settings/audit",
        "/settings/about",
        "/auth/profile",
    ]:
        assert client.get(path).status_code == 200, f"{path} did not render"


def test_api_endpoints(client, login):
    login()
    payload = client.get("/api/products?per_page=5").get_json()
    assert payload["ok"] is True
    assert payload["products"]

    sku = payload["products"][0]["sku"]
    lookup = client.get(f"/api/lookup?code={sku}").get_json()
    assert lookup["ok"] is True

    summary = client.get("/api/summary").get_json()
    assert summary["ok"] is True


def test_api_requires_authentication(app):
    with app.test_client() as anonymous:
        assert anonymous.get("/api/products").status_code == 401


def test_barcode_lookup_endpoint(client, login):
    login()
    created = client.post(
        "/products/new",
        data={"sku": "BAR-1", "name": "Barcoded item", "barcode": "999888777666",
              "cost": "2", "price": "5", "reorder_point": "5", "reorder_quantity": "10",
              "unit": "each", "is_active": "1"},
        follow_redirects=True,
    )
    assert "Barcoded item" in created.get_data(as_text=True)

    result = client.get("/scan/lookup?code=999888777666").get_json()
    assert result["ok"] is True
    assert result["sku"] == "BAR-1"


def test_duplicate_sku_is_rejected(client, login):
    login()
    client.post(
        "/products/new",
        data={"sku": "DUP-1", "name": "First", "cost": "1", "price": "2",
              "reorder_point": "1", "reorder_quantity": "1", "unit": "each"},
        follow_redirects=True,
    )
    response = client.post(
        "/products/new",
        data={"sku": "DUP-1", "name": "Second", "cost": "1", "price": "2",
              "reorder_point": "1", "reorder_quantity": "1", "unit": "each"},
        follow_redirects=True,
    )
    assert "already used by" in response.get_data(as_text=True)


def test_staff_cannot_reach_admin_pages(app, client, login):
    with app.app_context():
        user = User(
            username="clerk1", email="clerk1@test.local", full_name="Clerk One", role="staff"
        )
        user.set_password("Clerk@12345")
        db.session.add(user)
        db.session.commit()

    login("clerk1", "Clerk@12345")
    assert client.get("/users").status_code == 403
    assert client.get("/products/new").status_code == 403
    assert client.get("/products").status_code == 200

# --------------------------------------------------------------------------
# batch tracking and FEFO
# --------------------------------------------------------------------------
def _tracked_product(warehouse):
    db.session.add(warehouse)
    db.session.flush()
    product = Product(
        sku="TRK-1", name="Tracked consumable", unit="each",
        cost=Decimal("2.00"), price=Decimal("5.00"),
        track_batches=True, shelf_life_days=365,
    )
    db.session.add(product)
    db.session.flush()
    return product


def test_receipt_on_batch_product_needs_a_batch_number(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = _tracked_product(warehouse)

        with pytest.raises(StockError, match="batch number"):
            receive_stock(product, warehouse, 10)
        db.session.rollback()


def test_receipt_creates_batches_with_expiry(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = _tracked_product(warehouse)
        expiry = date.today() + timedelta(days=90)

        receive_stock(product, warehouse, 12, batch_no="B1", expiry_date=expiry)
        receive_stock(product, warehouse, 8, batch_no="B1", expiry_date=expiry)
        db.session.commit()

        batches = list(product.batches)
        assert len(batches) == 1
        assert batches[0].quantity == 20
        assert batches[0].expiry_date == expiry
        assert batches[0].expiry_state == "soon"
        assert product.quantity == 20


def test_fefo_consumes_the_earliest_expiry_first(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = _tracked_product(warehouse)
        today = date.today()

        receive_stock(product, warehouse, 20, batch_no="SOON",
                      expiry_date=today + timedelta(days=5), track_cost=False)
        receive_stock(product, warehouse, 10, batch_no="LATER",
                      expiry_date=today + timedelta(days=300), track_cost=False)
        db.session.commit()

        plan, short = allocate_batches(product, warehouse, 25)
        assert short == 0
        assert [(b.batch_no, q) for b, q in plan] == [("SOON", 20), ("LATER", 5)]

        issue_stock(product, warehouse, 25, reference="SALE")
        db.session.commit()

        remaining = {b.batch_no: b.quantity for b in product.batches}
        assert remaining == {"SOON": 0, "LATER": 5}
        assert product.quantity == 5


def test_issue_cannot_exceed_tracked_batches(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = _tracked_product(warehouse)
        receive_stock(product, warehouse, 4, batch_no="ONLY",
                      expiry_date=date.today() + timedelta(days=30), track_cost=False)
        db.session.commit()

        with pytest.raises(StockError):
            issue_stock(product, warehouse, 9)
        db.session.rollback()


def test_transfer_moves_the_same_batches(app):
    with app.app_context():
        source = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        destination = Warehouse(code="WH-02", name="Second site")
        db.session.add(destination)
        db.session.flush()

        product = _tracked_product(source)
        expiry = date.today() + timedelta(days=60)
        receive_stock(product, source, 30, batch_no="LOT-7",
                      expiry_date=expiry, track_cost=False)
        transfer_stock(product, source, destination, 12)
        db.session.commit()

        assert product.stock_at(source.id) == 18
        assert product.stock_at(destination.id) == 12

        moved = db.session.execute(
            db.select(Batch).where(
                Batch.product_id == product.id, Batch.warehouse_id == destination.id
            )
        ).scalar_one()
        assert moved.batch_no == "LOT-7"
        assert moved.quantity == 12
        assert moved.expiry_date == expiry


def test_adjustment_can_target_one_batch(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = _tracked_product(warehouse)
        receive_stock(product, warehouse, 15, batch_no="ADJ",
                      expiry_date=date.today() + timedelta(days=45), track_cost=False)
        db.session.commit()

        batch = next(iter(product.batches))
        item, delta = adjust_stock(product, warehouse, 11, batch_id=batch.id, note="damaged")
        db.session.commit()

        assert delta == -4
        assert batch.quantity == 11
        assert product.quantity == 11

        movement = db.session.execute(
            db.select(StockMovement).where(StockMovement.move_type == "adjustment")
        ).scalars().one()
        assert movement.batch_id == batch.id


def test_expiry_summary_splits_expired_and_upcoming(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        today = date.today()

        expired = _tracked_product(warehouse)
        expired.sku = "OLD-1"
        receive_stock(expired, warehouse, 5, batch_no="OLD", unit_cost=Decimal("2.00"),
                      expiry_date=today - timedelta(days=10), track_cost=False)

        soon = _tracked_product(warehouse)
        soon.sku = "NEW-1"
        receive_stock(soon, warehouse, 7, batch_no="NEW", unit_cost=Decimal("2.00"),
                      expiry_date=today + timedelta(days=20), track_cost=False)
        db.session.commit()

        summary = batch_summary(90)
        assert len(summary["expired"]) == 1
        assert summary["expired"][0].batch_no == "OLD"
        assert len(summary["expiring"]) == 1
        assert summary["expired_value"] > 0


# --------------------------------------------------------------------------
# reorder analytics
# --------------------------------------------------------------------------
def test_reorder_recommendation_uses_usage_and_lead_time(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product, warehouse = _make_product(sku="USE-1", name="Consumed item")
        supplier = Supplier(name="Lead Time Supplier", lead_time_days=10)
        db.session.add(supplier)
        product.supplier = supplier
        db.session.commit()

        receive_stock(product, warehouse, 300, track_cost=False)
        for day in range(0, 30, 3):
            issue_stock(product, warehouse, 6, reference=f"DAY-{day}")
        db.session.commit()

        recommendation = reorder_recommendation(product)
        assert recommendation is not None
        assert recommendation["lead_time"] == 10
        assert recommendation["service_level"] == 95
        assert recommendation["reorder_point"] > 0
        assert recommendation["reorder_quantity"] > 0
        assert recommendation["safety_stock"] >= 0


def test_reorder_recommendation_needs_history(app):
    with app.app_context():
        product, _ = _make_product(sku="NOUSE-1", name="Never issued")
        db.session.commit()
        assert reorder_recommendation(product) is None


def test_apply_recommendation_updates_the_product(client, login, app):
    login()
    with app.app_context():
        product, warehouse = _make_product(sku="REC-1", name="Recommendation subject")
        receive_stock(product, warehouse, 120, track_cost=False)
        for _ in range(10):
            issue_stock(product, warehouse, 5)
        db.session.commit()
        product_id = product.id

    response = client.post(f"/products/{product_id}/apply-recommendation", follow_redirects=True)
    assert "Reorder point set to" in response.get_data(as_text=True)

    with app.app_context():
        refreshed = db.session.get(Product, product_id)
        assert refreshed.reorder_point > 0
        assert refreshed.reorder_quantity > 0


# --------------------------------------------------------------------------
# translations
# --------------------------------------------------------------------------
def test_every_locale_translates_every_string():
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "extract_strings.py"), "--missing", "fi", "zh", "ja"],
        capture_output=True, text=True, cwd=root,
    )
    assert "0 missing" in result.stdout or result.stdout.strip() == "", result.stdout
    assert ": 0 missing of" in result.stdout, result.stdout


def test_translation_falls_back_to_english(app):
    from inventory.i18n import translate

    with app.test_request_context("/"):
        assert translate("Products") in {"Products", "Tuotteet", "产品", "製品"}
        assert translate("Totally unknown string") == "Totally unknown string"


def test_locale_can_be_switched_and_remembered(client, login):
    login()
    response = client.post("/language/fi", data={"next": "/products"}, follow_redirects=True)
    assert response.status_code == 200
    assert 'lang="fi"' in response.get_data(as_text=True)
    assert "Tuotteet" in response.get_data(as_text=True)

    again = client.get("/products")
    assert "Ohjausnäkymä" in again.get_data(as_text=True)

    client.post("/language/ja", data={"next": "/products"}, follow_redirects=True)
    assert "ダッシュボード" in client.get("/").get_data(as_text=True)


def test_locale_specific_number_and_date_formatting():
    from datetime import date

    from inventory.i18n.format import format_date, format_money, format_number

    assert format_money(1234.5, "en", "$") == "$1,234.50"
    assert format_money(1234.5, "fi", "$") == "$1\u00a0234,50"
    assert format_number(12345, "en") == "12,345"
    assert format_number(12345, "fi") == "12\u00a0345"
    assert format_date(date(2026, 3, 7), "en") == "07 Mar 2026"
    assert format_date(date(2026, 3, 7), "fi") == "07.03.2026"
    assert format_date(date(2026, 3, 7), "zh") == "2026-03-07"
    assert format_date(date(2026, 3, 7), "ja") == "2026/03/07"


def test_pages_render_in_every_language(client, login):
    login()
    for code in ("en", "fi", "zh", "ja"):
        client.post(f"/language/{code}", data={"next": "/"}, follow_redirects=True)
        for path in ("/", "/products", "/stock/movements", "/reports/expiry", "/settings"):
            response = client.get(path)
            assert response.status_code == 200, f"{path} failed in {code}"
            assert f'lang="{code}"' in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------
def test_dispatch_never_breaks_a_request(app):
    from inventory.notifier import dispatch

    with app.test_request_context("/"):
        set_setting("notify_webhook_url", "ftp://not-a-webhook")
        body = dispatch("stock.low", sku="X", name="Y")
        assert body["event"] == "stock.low"
        assert body["data"]["sku"] == "X"


def test_webhook_receives_low_stock_events(app):
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.handle_request, daemon=True).start()

    app.config["ALLOW_PRIVATE_WEBHOOKS"] = True
    with app.test_request_context("/"):
        set_setting("notify_webhook_url", f"http://127.0.0.1:{server.server_port}/hook")
        db.session.commit()

        product, warehouse = _make_product(sku="LOW-1", name="Low item", reorder_point=10)
        db.session.commit()
        receive_stock(product, warehouse, 2)
        db.session.commit()
        check_stock_levels([product])

    server.server_close()
    assert received, "webhook was never called"
    assert received[0]["event"] == "stock.low"
    assert received[0]["data"]["sku"] == "LOW-1"


def test_low_stock_event_is_emitted_for_out_of_stock(app):
    from inventory.notifier import EVENT_OUT_OF_STOCK

    calls = []
    import inventory.notifier as notifier

    original = notifier.dispatch
    notifier.dispatch = lambda event, **payload: calls.append((event, payload))
    try:
        with app.test_request_context("/"):
            product, _ = _make_product(sku="OUT-1", name="Out item", reorder_point=5)
            db.session.commit()
            check_stock_levels([product])
    finally:
        notifier.dispatch = original

    assert calls and calls[0][0] == EVENT_OUT_OF_STOCK


# --------------------------------------------------------------------------
# hardening added after the security review
# --------------------------------------------------------------------------
def test_login_is_throttled_after_repeated_failures(app):
    client = app.test_client()

    def token():
        body = client.get("/auth/login").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    for _ in range(5):
        client.post("/auth/login", data={"identity": "admin", "password": "wrong",
                                         "csrf_token": token()})

    body = client.post("/auth/login", data={"identity": "admin", "password": "wrong",
                                            "csrf_token": token()}).get_data(as_text=True)
    assert "Too many failed sign-ins" in body

    # the correct password is refused too while the lockout is active
    locked = client.post("/auth/login", data={"identity": "admin", "password": "Admin@12345",
                                              "csrf_token": token()}).get_data(as_text=True)
    assert "Too many failed sign-ins" in locked

    # a different identity from the same address is also held back
    other = client.post("/auth/login", data={"identity": "someoneelse", "password": "x",
                                             "csrf_token": token()}).get_data(as_text=True)
    assert "Too many failed sign-ins" in other

    with app.app_context():
        assert db.session.execute(db.select(db.func.count(LoginAttempt.id))).scalar_one() >= 5


def test_successful_login_clears_previous_session_data(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["planted"] = "attacker"
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/auth/login").get_data(as_text=True)).group(1)
    client.post("/auth/login", data={"identity": "admin", "password": "Admin@12345",
                                     "csrf_token": token})
    with client.session_transaction() as sess:
        assert "planted" not in sess


def test_setup_page_is_closed_once_installed(client):
    response = client.get("/auth/setup")
    assert response.status_code == 403
    assert "Setup is already complete" in response.get_data(as_text=True)


def test_security_headers_are_present(client, login):
    login()
    headers = client.get("/").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Referrer-Policy"] == "same-origin"
    assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    assert "object-src 'none'" in headers["Content-Security-Policy"]


def test_upload_size_is_capped(app):
    assert app.config["MAX_CONTENT_LENGTH"] == 5 * 1024 * 1024


def test_quantities_above_the_ceiling_are_refused(app):
    with app.app_context():
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product, _ = _make_product(sku="CAP-1", name="Capped")
        db.session.commit()
        start = product.quantity

        with pytest.raises(StockError, match="above the maximum"):
            receive_stock(product, warehouse, app.config["MAX_LINE_QUANTITY"] + 1)
        db.session.rollback()

        receive_stock(product, warehouse, 5)
        db.session.commit()
        assert product.quantity == start + 5


def test_webhook_rejects_internal_addresses(app):
    from inventory.notifier import UnsafeURLError, validate_webhook_url

    app.config["ALLOW_PRIVATE_WEBHOOKS"] = False
    with app.test_request_context("/"):
        for blocked in ("http://127.0.0.1/hook", "http://localhost/hook",
                        "http://169.254.169.254/latest/meta-data/",
                        "http://10.0.0.5/hook", "http://192.168.1.1/hook",
                        "ftp://example.com/hook", "file:///etc/passwd",
                        "http://user:pass@example.com/hook"):
            with pytest.raises(UnsafeURLError):
                validate_webhook_url(blocked)

        assert validate_webhook_url("https://93.184.216.34/x").endswith("/x")


def test_settings_page_refuses_an_internal_webhook(app):
    app.config["ALLOW_PRIVATE_WEBHOOKS"] = False
    client = sign_in(app)
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/settings").get_data(as_text=True)).group(1)
    client.post("/settings", data={"csrf_token": token, "company_name": "Co",
                                  "currency_symbol": "$",
                                  "notify_webhook_url": "http://169.254.169.254/latest/"})
    body = client.get("/settings").get_data(as_text=True)
    assert "private or reserved address" in body


def _find_product_id(application, sku):
    with application.app_context():
        return db.session.execute(
            db.select(Product).where(Product.sku == sku)
        ).scalar_one().id


def test_csv_export_escapes_formulas(app):
    from inventory.utils import csv_response

    payload, _ = csv_response("x.csv", ["a"], [["=cmd|calc"], ["+1+1"], ["-2+3"], ["@SUM(A1)"], ["ok"]])
    assert "'=cmd|calc" in payload
    assert "'+1+1" in payload
    assert "'-2+3" in payload
    assert "'@SUM(A1)" in payload
    assert "\nok" in payload

    session = sign_in(app)
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      session.get("/products/new").get_data(as_text=True)).group(1)
    session.post("/products/new", data={"csrf_token": token, "sku": "FORMULA-1",
                                        "name": "=1+1", "cost": "1", "price": "2",
                                        "reorder_point": "1", "reorder_quantity": "1"},
                 follow_redirects=False)
    export = session.get("/products/export.csv").get_data(as_text=True)
    assert "'=1+1" in export

    response = session.post(f"/products/{_find_product_id(app, 'FORMULA-1')}/edit",
                            data={"csrf_token": token, "sku": "=cmd|calc", "name": "evil",
                                  "cost": "1", "price": "2", "reorder_point": "1",
                                  "reorder_quantity": "1"}, follow_redirects=False)
    body = response.get_data(as_text=True)
    assert "may only contain letters, numbers, dots, dashes and underscores" in body


def test_debug_is_off_by_default():
    from config import Config

    assert Config.DEBUG is False


def test_secret_key_is_not_the_shipped_constant():
    from config import Config

    assert Config.SECRET_KEY != "dev-only-change-me"
    assert len(Config.SECRET_KEY) >= 32


# --------------------------------------------------------------------------
# toast placement and barcode rendering
# --------------------------------------------------------------------------
def test_centred_pages_stack_the_toast_above_the_card():
    """The login and setup pages use a column layout so the flash sits on top."""
    css = pathlib.Path(__file__).resolve().parent.parent.joinpath(
        "inventory/static/css/app.css"
    ).read_text()
    centred = css.split(".main--centered {")[1].split("}")[0]
    assert "flex-direction: column" in centred, "centred pages must stack their content"
    assert "align-items: center" in centred

    flash_rule = css.split(".main--centered .flashes {")[1].split("}")[0]
    assert "max-width" in flash_rule, "the toast needs a width on centred pages"

    wide_rule = css.split(".main--centered:has(.auth-card--wide) .flashes {")[1].split("}")[0]
    assert "max-width" in wide_rule, "the setup page uses a wider card"


def test_flash_partial_renders_inside_main(app):
    with app.test_client() as client:
        html = client.get("/auth/login").get_data(as_text=True)
        assert 'class="main main--centered"' in html
        assert "<main" in html
        # no messages yet, so the container is not emitted at all
        assert 'class="flashes"' not in html

        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        failed = client.post("/auth/login", data={"csrf_token": token,
                                                   "identity": "admin",
                                                   "password": "wrong"})
        shown = failed.get_data(as_text=True)
        assert 'class="flashes"' in shown
        assert 'alert alert--danger' in shown
        assert "do not match" in shown


def test_barcode_page_uses_the_local_encoder(app, login):
    login()
    body = app.test_client().get("/products/1/barcode").get_data(as_text=True)
    assert "/static/js/barcode.js" in body
    assert "jsdelivr" not in body, "barcode rendering must not depend on a CDN"
    assert 'id="barcode"' in body
    assert "InventoryBarcode.renderToCanvas" in body


def test_shelf_labels_print_scannable_codes(app, login):
    login()
    body = app.test_client().get("/products/labels").get_data(as_text=True)
    assert 'class="label__barcode"' in body
    assert "/static/js/barcode.js" in body
    assert "jsdelivr" not in body


def test_barcode_encoder_is_served(app):
    response = app.test_client().get("/static/js/barcode.js")
    assert response.status_code == 200
    assert b"EAN13" in response.data
    assert b"CODE128" in response.data


def test_barcode_encoder_unit_tests_pass():
    """Run the Node test suite when Node is available."""
    import shutil
    import subprocess

    node = shutil.which("node")
    root = pathlib.Path(__file__).resolve().parent.parent
    script = root / "tests" / "barcode.test.js"
    if not node or not script.exists():
        return
    result = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_scan_lookup_works_without_the_camera_library(app, login):
    """The scan desk resolves codes server side, so a missing camera library is survivable."""
    login()
    client = app.test_client()
    result = client.get("/scan/lookup?code=CLN-100").get_json()
    assert result["ok"] is True
    assert result["sku"] == "CLN-100"
    assert result["on_hand"] > 0
    assert "WH-01" in result["by_warehouse"]

    missing = client.get("/scan/lookup?code=nothing-here")
    assert missing.status_code == 404
    assert missing.get_json()["ok"] is False

    page = client.get("/scan").get_data(as_text=True)
    assert 'id="codeInput"' in page
    assert 'id="warehouseSelect"' in page
    assert "/scan/lookup" in page


# --------------------------------------------------------------------------
# scanning a printed label back to the product
# --------------------------------------------------------------------------
def test_barcode_variants_cover_the_forms_a_reader_returns():
    assert barcode_variants("400638133393")[:2] == [
        "400638133393", "4006381333931"
    ]
    assert "400638133393" in barcode_variants("4006381333931")
    assert "5012345678900" in barcode_variants("501234567890")
    assert "4006381333931" in barcode_variants("40 0638 133393")
    assert "4006381333931" in barcode_variants("40-0638-133393")
    # a reader that drops the leading zero of a zero padded EAN still matches
    assert "641234567893" in barcode_variants("0641234567893")
    assert "0641234567893" in barcode_variants("641234567893")
    assert barcode_variants("") == []
    assert barcode_variants(None) == []
    # an ordinary code is left alone
    assert barcode_variants("PCB-001") == ["PCB-001"]


def test_scanning_a_printed_label_finds_the_product(app, login):
    """A label prints the full 13 digit EAN, so the scan must accept it."""
    login()
    product, _ = _make_product(sku="SCAN-1", barcode="400638133393")
    db.session.commit()
    client = app.test_client()

    for code in ("400638133393", "4006381333931", "40 0638 133393"):
        result = client.get("/scan/lookup", query_string={"code": code})
        assert result.status_code == 200, f"{code} did not resolve"
        assert result.get_json()["sku"] == "SCAN-1"


def test_every_scan_entry_point_normalises_the_code(app, login):
    login()
    product, _ = _make_product(sku="SCAN-2", barcode="501234567890")
    db.session.commit()
    client = app.test_client()
    assert client.get("/scan/lookup?code=5012345678900").get_json()["sku"] == "SCAN-2"
    assert client.get("/products/lookup?code=5012345678900").get_json()["sku"] == "SCAN-2"
    api = client.get("/api/lookup?code=5012345678900")
    assert api.status_code == 200, "api lookup did not resolve"
    assert api.get_json()["product"]["sku"] == "SCAN-2"


def test_a_13_digit_barcode_is_not_reusable_on_another_product(app, client, login):
    """The same number written with and without its check digit is one code."""
    login()
    _make_product(sku="DUP-1", barcode="400638133393")
    db.session.commit()

    response = client.post(
        "/products/new",
        data={"sku": "DUP-2", "name": "Twin", "unit": "each", "cost": 1,
              "price": 2, "reorder_point": 1, "reorder_quantity": 1,
              "barcode": "4006381333931"},
    )
    assert "4006381333931" in response.get_data(as_text=True)

    from inventory.models import Product
    with app.app_context():
        clash = db.session.execute(
            db.select(Product).where(Product.sku == "DUP-2")
        ).scalars().first()
    assert clash is None, "a duplicate barcode in another form was accepted"
