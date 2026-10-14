import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def engine_options(uri):
    """Driver specific connect arguments.

    SQLite and psycopg reject each other's options, so only SQLite gets the
    thread and timeout settings.
    """
    options = {"pool_pre_ping": True}
    if uri.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return options


def resolve_secret_key():
    """Return a secret key, generating and persisting one when none is given.

    A key that is not in the source control is the whole session security model,
    so an unset key must never fall back to a shipped constant.
    """
    from_env = os.environ.get("SECRET_KEY", "").strip()
    if from_env:
        return from_env

    key_file = os.path.join(INSTANCE_DIR, "secret_key")
    try:
        if os.path.exists(key_file):
            with open(key_file, encoding="utf-8") as handle:
                stored = handle.read().strip()
            if stored:
                return stored
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        with open(key_file, "w", encoding="utf-8") as handle:
            handle.write(generated)
        os.chmod(key_file, 0o600)
        return generated
    except OSError:
        return secrets.token_urlsafe(48)


class Config:
    SECRET_KEY = resolve_secret_key()

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'inventory.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = engine_options(
        os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'inventory.db')}")
    )

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_DAYS", "7")))
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)

    WTF_CSRF_TIME_LIMIT = None
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "5")) * 1024 * 1024

    ITEMS_PER_PAGE = int(os.environ.get("ITEMS_PER_PAGE", "20"))

    DEBUG = _bool("FLASK_DEBUG", False)

    MAX_LINE_QUANTITY = int(os.environ.get("MAX_LINE_QUANTITY", "1000000"))
    LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", "15"))
    TRUST_PROXY_HEADERS = _bool("TRUST_PROXY_HEADERS", False)
    ALLOW_PRIVATE_WEBHOOKS = _bool("ALLOW_PRIVATE_WEBHOOKS", False)

    CSP_SCRIPT_SRC = "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
    CSP_STYLE_SRC = "'self' 'unsafe-inline'"


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "testing-secret"
    DEBUG = False
    LOGIN_MAX_ATTEMPTS = 5
    LOGIN_LOCKOUT_MINUTES = 15


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", True)
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE