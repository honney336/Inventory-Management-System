#!/bin/sh
set -e

wait_for_database() {
    python - <<'PY'
import os
import sys
import time
import urllib.parse

url = os.environ.get("DATABASE_URL", "")
if not url.startswith("postgres"):
    sys.exit(0)

try:
    import psycopg
except ImportError:
    print("psycopg is not installed, skipping the wait")
    sys.exit(0)

parsed = urllib.parse.urlparse(url.replace("postgresql+psycopg", "postgresql"))
target = {
    "dbname": (parsed.path or "/postgres").lstrip("/") or "postgres",
    "user": urllib.parse.unquote(parsed.username or ""),
    "password": urllib.parse.unquote(parsed.password or ""),
    "host": parsed.hostname or "db",
    "port": parsed.port or 5432,
}

for attempt in range(1, 61):
    try:
        with psycopg.connect(connect_timeout=3, **target):
            print(f"database ready after {attempt} attempt(s)")
            sys.exit(0)
    except Exception as exc:
        last = exc
        time.sleep(1)

print(f"database not ready: {last}")
sys.exit(1)
PY
}

ensure_schema() {
    python - <<'PY'
from inventory import create_app
from inventory.extensions import db
from inventory.services import init_settings

app = create_app()
with app.app_context():
    db.create_all()
    init_settings()
print("schema ready", flush=True)
PY
}

case "${1:-}" in
    serve)
        wait_for_database
        ensure_schema
        exec gunicorn \
            --bind "0.0.0.0:${PORT:-8000}" \
            --workers "${WEB_CONCURRENCY:-3}" \
            --threads "${WEB_THREADS:-4}" \
            --timeout "${GUNICORN_TIMEOUT:-60}" \
            --access-logfile - \
            --error-logfile - \
            "app:create_app(\"${FLASK_CONFIG:-production}\")"
        ;;
    init)
        wait_for_database
        ensure_schema
        ;;
    seed)
        wait_for_database
        ensure_schema
        exec python -c "from inventory.seed import run_seed; run_seed(demo=True)"
        ;;
    check-translations)
        exec flask --app app check-translations
        ;;
    flask)
        shift
        exec flask --app app "$@"
        ;;
    *)
        exec "$@"
        ;;
esac