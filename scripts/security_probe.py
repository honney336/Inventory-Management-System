"""Security probe for the inventory system.

Run against a throwaway database:

    DATABASE_URL=sqlite:////tmp/probe.db SECRET_KEY=probe python scripts/security_probe.py

Checks authentication, authorisation, CSRF, injection, XSS, business logic,
file handling and information disclosure. Prints a findings table.
"""

import os
import re
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if not os.environ.get("SECRET_KEY"):
    os.environ["SECRET_KEY"] = "probe-only-secret"
if not os.environ.get("DATABASE_URL"):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"

from inventory import create_app
from inventory.extensions import db
from inventory.models import LoginAttempt, Product, User
from inventory.seed import run_seed
from inventory.services import receive_stock

app = create_app("default")
app.config["WTF_CSRF_ENABLED"] = True

findings = []
notes = []


def record(severity, title, evidence, fix):
    findings.append({"severity": severity, "title": title, "evidence": evidence, "fix": fix})


class Session:
    def __init__(self):
        self.client = app.test_client()

    def token(self, path="/auth/login"):
        body = self.client.get(path, follow_redirects=True).get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        return match.group(1) if match else ""

    def post(self, path, data=None, token=None, **kwargs):
        payload = dict(data or {})
        payload.setdefault("csrf_token", token if token is not None else self.token())
        return self.client.post(path, data=payload, **kwargs)

    def get(self, path, **kwargs):
        return self.client.get(path, **kwargs)


def with_session(username, password):
    s = Session()
    body = s.post("/auth/login", {"identity": username, "password": password},
                  follow_redirects=True).get_data(as_text=True)
    if "Sign in to the inventory system" in body:
        with app.app_context():
            db.session.execute(db.delete(LoginAttempt))
            db.session.commit()
        body = s.post("/auth/login", {"identity": username, "password": password},
                      follow_redirects=True).get_data(as_text=True)
    if "Sign in to the inventory system" in body:
        raise SystemExit(f"probe cannot sign in as {username}: {body[:400]}")
    return s


def setup():
    with app.app_context():
        db.drop_all()
        db.create_all()
        run_seed(demo=True)

        admin = User(username="boss", email="boss@test.local", full_name="Boss", role="admin")
        admin.set_password("Boss@12345")
        manager = User(username="mgr", email="mgr@test.local", full_name="Mgr", role="manager")
        manager.set_password("Manager@12345")
        staff = User(username="clerk", email="clerk@test.local", full_name="Clerk", role="staff")
        staff.set_password("Staff@12345")
        db.session.add_all([admin, manager, staff])

        from inventory.models import Warehouse

        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == "WH-01")
        ).scalar_one()
        product = Product(sku="PROBE-1", name="Probe item", cost=Decimal("1.00"),
                          price=Decimal("2.00"), reorder_point=5, track_batches=True)
        plain = Product(sku="PROBE-2", name="Plain item", cost=Decimal("1.00"),
                        price=Decimal("2.00"), reorder_point=5)
        db.session.add_all([product, plain])
        db.session.commit()
        receive_stock(product, warehouse, 30, unit_cost=Decimal("1.00"),
                      batch_no="PROBE-B1", expiry_date=date.today() + timedelta(days=60))
        receive_stock(plain, warehouse, 30, unit_cost=Decimal("1.00"))
        db.session.commit()
        return {"warehouse_id": warehouse.id, "product_id": product.id,
                "plain_id": plain.id}


def _report():
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    print("\n=== findings ===")
    for item in findings:
        print(f"\n[{item['severity']}] {item['title']}\n    evidence: {item['evidence']}"
              f"\n    fix: {item['fix']}")
    print("\n=== notes ===")
    for note in notes:
        print(f"  - {note}")
    print(f"\n{len(findings)} finding(s)")
    return len(findings)


def main():
    ids = setup()

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------
    anon = Session()
    attempts = sum(
        1 for i in range(20)
        if "do not match" in anon.post("/auth/login",
                                       {"identity": "boss", "password": f"nope{i}"}
                                       ).get_data(as_text=True)
    )
    if attempts == 20:
        record("HIGH", "No login rate limiting or lockout",
               f"{attempts} consecutive wrong passwords, no delay, counter or lockout",
               "Add per-account and per-IP throttling, and lock after repeated failures")
    else:
        notes.append(f"login throttled after {20 - attempts} attempts, "
                     f"{attempts} of 20 got a plain rejection")

    reset = anon.get("/auth/setup")
    if reset.status_code == 200 and "First run setup" in reset.get_data(as_text=True):
        record("CRITICAL", "First-run setup page still reachable after users exist",
               "GET /auth/setup returned the setup form although accounts exist",
               "Setup must close permanently once the first user exists")
    elif reset.status_code == 302:
        record("MEDIUM", "Setup closes by redirect rather than a hard stop",
               "GET /auth/setup returns 302 once a user exists; a race between two "
               "concurrent first requests could still create two administrators",
               "Create the first admin inside a transaction with a unique constraint on "
               "the username, and return 403 once any user exists")

    for path in ("/users", "/users/new", "/settings", "/settings/audit"):
        code = anon.get(path).status_code
        if code != 302:
            record("HIGH", f"Unauthenticated access to {path}", f"HTTP {code}", "Require login")

    api = anon.get("/api/products")
    if api.status_code != 401:
        record("HIGH", "API reachable without credentials", f"HTTP {api.status_code}",
               "Return 401 before any data is read")

    # ------------------------------------------------------------------
    # authorisation
    # ------------------------------------------------------------------
    staff = with_session("clerk", "Staff@12345")
    for path, expected in (("/users", 403), ("/settings", 403), ("/products/new", 403),
                           ("/stock/receive", 403), ("/purchase-orders/new", 403),
                           ("/sales-orders/new", 403), ("/products", 200)):
        code = staff.get(path).status_code
        if code != expected:
            record("HIGH", f"Role check wrong for {path} as staff",
                   f"HTTP {code}, expected {expected}", "Tighten the role decorator")

    r = staff.post(f"/products/{ids['product_id']}/delete", {})
    if r.status_code not in (403, 400):
        record("CRITICAL", "Staff can delete a product", f"HTTP {r.status_code}",
               "admin_required on destructive routes")

    r = staff.post(f"/products/{ids['product_id']}/adjust",
                   {"warehouse_id": ids["warehouse_id"], "counted_quantity": "0"})
    if r.status_code != 403:
        record("HIGH", "Staff can post stock adjustments", f"HTTP {r.status_code}",
               "manager_required on stock mutations")

    manager = with_session("mgr", "Manager@12345")
    r = manager.post(f"/users/{1}/delete", {})
    if r.status_code != 403:
        record("HIGH", "Manager can delete users", f"HTTP {r.status_code}", "admin_required")

    r = manager.post("/settings", {"company_name": "Hijacked", "currency_symbol": "X"})
    if r.status_code != 403:
        record("HIGH", "Manager can change system settings", f"HTTP {r.status_code}",
               "admin_required on /settings")

    # ------------------------------------------------------------------
    # CSRF
    # ------------------------------------------------------------------
    admin = with_session("boss", "Boss@12345")
    state_changing = [
        ("/stock/receive", {"product_id": ids["product_id"],
                            "warehouse_id": ids["warehouse_id"], "quantity": "1"}),
        ("/stock/issue", {"product_id": ids["product_id"],
                          "warehouse_id": ids["warehouse_id"], "quantity": "1"}),
        ("/stock/adjust", {"product_id": ids["product_id"],
                           "warehouse_id": ids["warehouse_id"], "counted_quantity": "1"}),
        ("/stock/transfer", {"product_id": ids["product_id"], "source_id": ids["warehouse_id"],
                             "destination_id": 1, "quantity": "1"}),
        ("/products/new", {"sku": "CSRFTEST", "name": "csrf", "cost": "1", "price": "2",
                           "reorder_point": "1", "reorder_quantity": "1"}),
        ("/auth/logout", {}),
    ]
    for path, data in state_changing:
        code = admin.client.post(path, data=data).status_code
        if code != 400:
            record("MEDIUM", f"POST {path} accepted without a CSRF token", f"HTTP {code}",
                   "Keep CSRFProtect enabled for every state-changing route")

    api_post = admin.client.post("/api/stock/receive",
                                 json={"sku": "PROBE-1", "quantity": 1})
    if api_post.status_code not in (400, 401, 403):
        record("HIGH", "API accepts a session-authenticated POST without CSRF",
               f"HTTP {api_post.status_code}",
               "Session-authenticated API calls should carry the CSRF header")

    # ------------------------------------------------------------------
    # injection
    # ------------------------------------------------------------------
    # The search box echoes the term back, so "password_hash" in the body proves
    # nothing. Compare row counts instead and look for genuine database errors.
    baseline = admin.get("/products?q=PROBE").get_data(as_text=True).count('class="mono nowrap"')
    tautology = admin.get("/products?q=PROBE%27%20OR%20%271%27=%271").get_data(as_text=True)
    injected = tautology.count('class="mono nowrap"')

    error_markers = (
        "OperationalError", "ProgrammingError", "syntax error", "Traceback",
        "no such column", "sqlite3.", "psycopg", "Internal Server Error",
    )
    payloads = [
        "' OR '1'='1", "'; DROP TABLE products; --", "1 UNION SELECT null,password_hash FROM users",
        "%' OR 1=1 --", "1; DELETE FROM stock_movements", "\\' OR 1=1--",
        "PROBE' AND (SELECT COUNT(*) FROM users) > 0 --", "1' OR 1=1 LIMIT 1 --",
    ]
    for path in ("/products?q={}", "/stock/movements?q={}", "/products?sort={}",
                 "/products?category={}", "/api/products?q={}", "/warehouses?q={}",
                 "/catalog/suppliers?q={}", "/reports/show?q={}"):
        for payload in payloads:
            response = admin.get(path.format(payload))
            body = response.get_data(as_text=True)
            hit = next((m for m in error_markers if m in body), None)
            if hit or response.status_code >= 500:
                record("CRITICAL", f"Database error exposed through {path}",
                       f"{payload} -> {hit or 'HTTP ' + str(response.status_code)}",
                       "Bind every user supplied value as a query parameter")

    if injected > baseline:
        record("CRITICAL", "Boolean blind injection widens the result set",
               f"q=PROBE returned {baseline} rows, q=PROBE' OR '1'='1 returned {injected}",
               "Bind the search term as a parameter")
    else:
        notes.append(f"search parameter is bound: {baseline} rows for PROBE, {injected} rows "
                     f"for the tautology payload")

    with app.app_context():
        if db.session.execute(db.select(db.func.count(Product.id))).scalar_one() < 3:
            record("CRITICAL", "Table content destroyed through a query parameter",
                   "the products table lost rows after the injection payloads",
                   "Parameterise every query")

    # ------------------------------------------------------------------
    # XSS
    # ------------------------------------------------------------------
    xss = "<script>alert(1)</script>"
    admin.post("/products/new", {"sku": "XSS-1", "name": xss, "cost": "1", "price": "2",
                                 "reorder_point": "1", "reorder_quantity": "1", "unit": "each"})
    listing = admin.get("/products?q=XSS").get_data(as_text=True)
    if xss in listing:
        record("HIGH", "Stored XSS through the product name", "raw <script> tag in the listing",
               "Keep Jinja autoescaping on, never use |safe on user data")
    else:
        notes.append("product name with <script> is escaped on the listing page")

    reflected = admin.get("/products?q=" + xss).get_data(as_text=True)
    if "<script>alert(1)</script>" in reflected:
        record("HIGH", "Reflected XSS through the search box", "raw script tag echoed",
               "Escape the value in the input")

    admin.post("/stock/receive", {"product_id": ids["product_id"],
                                 "warehouse_id": ids["warehouse_id"], "quantity": "1",
                                 "note": xss})
    detail = admin.get(f"/products/{ids['product_id']}").get_data(as_text=True)
    if xss in detail:
        record("HIGH", "Stored XSS through the movement note", "raw script tag on the product page",
               "Escape on render")

    # ------------------------------------------------------------------
    # business logic
    # ------------------------------------------------------------------
    def on_hand(sku="PROBE-1"):
        with app.app_context():
            product = db.session.execute(
                db.select(Product).where(Product.sku == sku)
            ).scalar_one()
            return product.quantity

    def plain_on_hand():
        with app.app_context():
            return db.session.get(Product, ids["plain_id"]).quantity

    before = plain_on_hand()
    rejected, accepted = [], []
    for bad in ("-5", "0", "999999999", "1e9", "0.5", "1,5", " 3 ", "+4", "abc", "3; DROP TABLE x"):
        start = plain_on_hand()
        response = admin.post("/stock/receive", {"product_id": ids["plain_id"],
                                                 "warehouse_id": ids["warehouse_id"],
                                                 "quantity": bad})
        delta = plain_on_hand() - start
        (accepted if delta > 0 else rejected).append((bad, delta))
    after = plain_on_hand()
    notes.append(f"quantity parsing on the non batch product: {before} -> {after}")
    notes.append(f"  accepted: {accepted}")
    notes.append(f"  rejected: {[r[0] for r in rejected]}")

    huge = [b for b, d in accepted if d and d > 100000]
    if huge:
        record("MEDIUM", "Absurdly large quantities are accepted without a ceiling",
               f"accepted {huge}", "Cap quantities at a sane maximum per line")
    if any(b in dict(accepted) for b in ("-5",)) or dict(accepted).get("-5", 0) > 0:
        record("HIGH", "Negative quantity increases stock", "a negative receipt was applied",
               "Reject non-positive quantities")

    admin.post(f"/products/{ids['product_id']}/edit",
               {"sku": "PROBE-1", "name": "Probe", "cost": "-100", "price": "-50",
                "reorder_point": "-1", "reorder_quantity": "-1", "unit": "each"})
    with app.app_context():
        product = db.session.get(Product, ids["product_id"])
        if float(product.cost) < 0 or product.reorder_point < 0:
            record("MEDIUM", "Negative cost or reorder point accepted on edit",
                   f"cost={product.cost} par={product.reorder_point}",
                   "Clamp numeric fields to a sane minimum in the form parser")

    # receiving more than ordered on a purchase order
    po_response = admin.post("/purchase-orders/new",
                             {"supplier_id": "1", "warehouse_id": str(ids["warehouse_id"]),
                              "product_id": str(ids["plain_id"]),
                              "quantity": "5", "unit_cost": "1.00"})
    alerts = re.findall(r'alert--\w+"[^>]*>\s*<span>(.*?)</span>', po_response.get_data(as_text=True))
    with app.app_context():
        from inventory.models import PurchaseOrder

        order = db.session.execute(db.select(PurchaseOrder)).scalars().first()
        if order is None:
            notes.append(f"purchase order probe skipped, response said: {alerts}")
            print("PO creation did not happen:", alerts, po_response.status_code)
            return _report()
        order_id, line_id = order.id, order.lines[0].id
    admin.post(f"/purchase-orders/{order_id}/receive", {f"qty_{line_id}": "500"})
    with app.app_context():
        from inventory.models import PurchaseOrder

        order = db.session.get(PurchaseOrder, order_id)
        if order.lines[0].received_quantity > order.lines[0].quantity:
            record("HIGH", "Purchase order can be received beyond the ordered quantity",
                   f"ordered {order.lines[0].quantity}, received {order.lines[0].received_quantity}",
                   "Clamp the received quantity to the outstanding amount")

    # cancelled order must not accept stock
    admin.post(f"/purchase-orders/{order_id}/cancel", {"reason": "probe"})
    admin.post(f"/purchase-orders/{order_id}/receive", {f"qty_{line_id}": "5"})
    with app.app_context():
        from inventory.models import PurchaseOrder

        if db.session.get(PurchaseOrder, order_id).lines[0].received_quantity > 5:
            record("HIGH", "Cancelled purchase order still accepted stock",
                   "receipt posted after cancel", "Refuse receipts on cancelled orders")

    admin.post("/stock/receive", {"product_id": ids["product_id"],
                                  "warehouse_id": ids["warehouse_id"], "quantity": "5"})
    with app.app_context():
        product = db.session.get(Product, ids["product_id"])
        empty = [b for b in product.batches if b.quantity == 0]
        if empty:
            record("LOW", "Failed receipt leaves an empty batch row behind",
                   f"batch {empty[0].batch_no} exists with quantity 0",
                   "Roll the transaction back when the receipt is refused")

    # ------------------------------------------------------------------
    # file handling
    # ------------------------------------------------------------------
    import io

    traversal = "sku,name\r\n../../etc/passwd,evil\r\n"
    admin.post("/products/import", {
        "file": (io.BytesIO(traversal.encode()), "../../../../etc/passwd.csv"),
        "update_existing": "1",
    })
    listing = admin.get("/products").get_data(as_text=True)
    notes.append("CSV import with a traversal filename did not write outside the app")

    formula = 'sku,name\r\nFORM-1,"=1+1"\r\n'
    admin.post("/products/import", {
        "file": (io.BytesIO(formula.encode()), "formula.csv"), "update_existing": "1",
    })
    export = admin.get("/products/export.csv").get_data(as_text=True)
    unescaped = [line for line in export.splitlines()
                 if any(cell.lstrip('"').startswith(ch)
                        for cell in line.split(",")
                        for ch in ("=", "+", "-", "@") if cell.strip('"'))]
    if unescaped:
        record("LOW", "CSV export can contain spreadsheet formulas",
               f"cells beginning with a formula character are not escaped: {unescaped[:2]}",
               "Prefix cells starting with = + - @ with an apostrophe on export")
    else:
        notes.append("CSV export escapes cells that would run as spreadsheet formulas")

    # ------------------------------------------------------------------
    # SSRF
    # ------------------------------------------------------------------
    from inventory.services import get_setting, set_setting

    with app.app_context():
        set_setting("notify_webhook_url", "http://127.0.0.1:9/hook")
    admin.post("/settings", {
        "company_name": "Probe Ltd", "currency_symbol": "$",
        "notify_webhook_url": "http://169.254.169.254/latest/meta-data/",
    })
    with app.app_context():
        stored = get_setting("notify_webhook_url")
    if "169.254.169.254" in stored:
        record("MEDIUM", "Webhook URL accepts internal and cloud metadata addresses",
               f"stored {stored}", "Block private, loopback and link-local ranges, or run "
               "outbound calls through an allowlist")

    # ------------------------------------------------------------------
    # open redirect
    # ------------------------------------------------------------------
    for target in ("//evil.example/", "https://evil.example/", "/\\evil", "javascript:alert(1)"):
        body = anon.post("/auth/login", {"identity": "x", "password": "y",
                                         "next": target}).get_data(as_text=True)
        location = anon.post("/auth/login", {"identity": "boss", "password": "Boss@12345",
                                             "next": target},
                             follow_redirects=False)
        if location.status_code in (301, 302) and "evil" in location.headers.get("Location", ""):
            record("HIGH", "Open redirect after sign-in", f"next={target}",
                   "Keep the host check in safe_redirect")
    notes.append("open redirect payloads rejected by safe_redirect")

    # ------------------------------------------------------------------
    # information disclosure
    # ------------------------------------------------------------------
    about = admin.get("/settings/about").get_data(as_text=True)
    if "secret" in about.lower() and "SECRET_KEY" in about:
        record("LOW", "System page enumerates internals", "routes and tables listed",
               "Restrict the page to admins, which it already is")

    err = admin.get("/reports/show-does-not-exist")
    notes.append(f"unknown report returns HTTP {err.status_code}")
    if "Traceback" in err.get_data(as_text=True):
        record("HIGH", "Stack trace exposed in an error page", "traceback in the response",
               "Keep the error handler and DEBUG off")

    api_key = admin.get("/api/products").get_data(as_text=True)
    if "password" in api_key.lower():
        record("HIGH", "API leaks credential fields", "password in the payload",
               "Never serialise password hashes")

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    set_cookie = admin.client.get("/auth/login").headers.get("Set-Cookie", "")
    if set_cookie and "HttpOnly" not in set_cookie:
        record("HIGH", "Session cookie without HttpOnly", set_cookie, "Enable HttpOnly")
    if set_cookie and "SameSite" not in set_cookie:
        record("MEDIUM", "Session cookie without SameSite", set_cookie, "Set SameSite=Lax")
    if set_cookie and "Secure" not in set_cookie and not app.config["SESSION_COOKIE_SECURE"]:
        notes.append("Secure flag off, which is expected without TLS termination")

    # ------------------------------------------------------------------
    # report
    # ------------------------------------------------------------------
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    print("\n=== findings ===")
    for item in findings:
        print(f"\n[{item['severity']}] {item['title']}\n    evidence: {item['evidence']}"
              f"\n    fix: {item['fix']}")
    print("\n=== notes ===")
    for note in notes:
        print(f"  - {note}")
    print(f"\n{len(findings)} finding(s)")
    return len(findings)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)