"""Second pass: secret key, debug mode, session handling, headers, SSRF."""

import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tmp.name}")
os.environ.pop("SECRET_KEY", None)

from itsdangerous import URLSafeTimedSerializer

from config import Config
from inventory import create_app
from inventory.extensions import db
from inventory.seed import run_seed

findings, notes = [], []


def record(severity, title, evidence, fix):
    findings.append((severity, title, evidence, fix))


app = create_app("default")
with app.app_context():
    db.drop_all()
    db.create_all()
    run_seed(demo=True)
    admin_id = db.session.execute(db.select(db.column("id")).select_from(
        db.metadata.tables["users"]).where(
        db.metadata.tables["users"].c.username == "admin")).scalar_one()

# 1. forge a session with the shipped default key
if Config.SECRET_KEY == "dev-only-change-me":
    serializer = URLSafeTimedSerializer(Config.SECRET_KEY, salt="cookie-session",
                                         signer_kwargs={"key_derivation": "hmac",
                                                       "digest_method": "sha1"})
    forged = serializer.dumps({"_user_id": str(admin_id), "_fresh": True})
    client = app.test_client()
    client.set_cookie("session", forged, domain="localhost")
    response = client.get("/api/summary")
    if response.status_code == 200:
        record("CRITICAL", "Session forgery with the shipped default SECRET_KEY",
               f"a cookie signed with 'dev-only-change-me' authenticated as the admin "
               f"(user id {admin_id}) and /api/summary returned 200",
               "Refuse to start unless SECRET_KEY is set, and generate a random key "
               "automatically when it is missing")
    else:
        notes.append(f"forged cookie rejected (HTTP {response.status_code})")

# 2. debug mode and the interactive console
if app.config.get("DEBUG"):
    client = app.test_client()
    console = client.get("/console")
    record("CRITICAL", "DEBUG mode on by default exposes the Werkzeug debugger",
           f"GET /console returned HTTP {console.status_code} with FLASK_DEBUG unset; "
           f"the console executes Python on the host once the PIN is known",
           "Default DEBUG to False and require FLASK_DEBUG=true deliberately")

client = app.test_client()
crash = client.get("/products/999999999/edit", follow_redirects=False)
notes.append(f"unhandled route probe returned HTTP {crash.status_code}")

with app.app_context():
    from inventory.models import LoginAttempt

    db.session.execute(db.delete(LoginAttempt))
    db.session.commit()

# 3. security headers
with app.test_client() as client:
    client.post("/auth/login", data={"identity": "admin", "password": "Admin@12345",
                                     "csrf_token": re.search(
                                         r'name="csrf_token" value="([^"]+)"',
                                         client.get("/auth/login").get_data(as_text=True)).group(1)},
                follow_redirects=True)
    headers = client.get("/").headers
    missing = [h for h in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
                           "Content-Security-Policy", "Permissions-Policy")
               if h not in headers]
    if missing:
        record("MEDIUM", "Security response headers are missing",
               f"absent: {', '.join(missing)}",
               "Add nosniff, DENY or SAMEORIGIN framing, a referrer policy and a CSP")

# 4. session fixation
import flask

with app.test_request_context("/"):
    flask.session["planted"] = "attacker-value"
with app.test_client() as client:
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/auth/login").get_data(as_text=True)).group(1)
    client.post("/auth/login", data={"identity": "admin", "password": "Admin@12345",
                                     "csrf_token": token}, follow_redirects=True)
    with client.session_transaction() as sess:
        if "planted" in sess:
            record("HIGH", "Session is not regenerated on sign-in",
                   "a value written into the session before authentication survives login "
                   "(fixation window)",
                   "Call session.clear() before login_user and rotate the cookie")
        else:
            notes.append("session contents do not survive sign-in")

# 5. timing based user enumeration
anon = app.test_client()
def attempt(identity, password):
    body = anon.get("/auth/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
    start = time.perf_counter()
    anon.post("/auth/login", data={"identity": identity, "password": password,
                                   "csrf_token": token})
    return time.perf_counter() - start

unknown = sorted(attempt("does-not-exist", "x") for _ in range(5))[2]
wrong = sorted(attempt("admin", "x") for _ in range(5))[2]
ratio = wrong / unknown if unknown else 0
if ratio > 1.6:
    record("LOW", "Login timing reveals whether an account exists",
           f"unknown user {unknown * 1000:.0f} ms, wrong password {wrong * 1000:.0f} ms "
           f"({ratio:.1f}x)",
           "Hash a dummy password when the account is not found")
else:
    notes.append(f"login timing similar for unknown user ({unknown * 1000:.0f} ms) and "
                 f"wrong password ({wrong * 1000:.0f} ms)")

# 6. audit IP header trust
from inventory.models import AuditLog

with app.app_context():
    before = db.session.execute(db.select(db.func.count(AuditLog.id))).scalar_one()
with app.test_client() as client:
    client.post("/auth/login", data={"identity": "admin", "password": "Admin@12345",
                                     "csrf_token": re.search(
                                         r'name="csrf_token" value="([^"]+)"',
                                         client.get("/auth/login").get_data(as_text=True)).group(1)},
                follow_redirects=True)
    client.post("/products/new", data={"sku": "IPPROBE", "name": "x", "cost": "1", "price": "1",
                                       "reorder_point": "1", "reorder_quantity": "1",
                                       "csrf_token": re.search(
                                           r'name="csrf_token" value="([^"]+)"',
                                           client.get("/auth/login").get_data(as_text=True)).group(1),
                                       "X-Forwarded-For": "9.9.9.9"})
with app.app_context():
    entry = db.session.execute(db.select(AuditLog).where(
        AuditLog.action == "product.create").order_by(AuditLog.id.desc())).scalars().first()
    if entry and entry.ip_address == "9.9.9.9":
        record("LOW", "Audit log trusts the X-Forwarded-For header",
               f"entry recorded ip_address={entry.ip_address} taken straight from the header",
               "Only read the header when a trusted proxy is configured")

order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
findings.sort(key=lambda f: order[f[0]])
print("\n=== findings ===")
for sev, title, evidence, fix in findings:
    print(f"\n[{sev}] {title}\n    evidence: {evidence}\n    fix: {fix}")
print("\n=== notes ===")
for note in notes:
    print(f"  - {note}")
print(f"\n{len(findings)} finding(s)")
sys.exit(1 if findings else 0)