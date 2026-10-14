import os
from datetime import date

from dotenv import load_dotenv
from flask import Flask, g, render_template, request, session
from flask_login import current_user

from .i18n import translate as _

from config import BASE_DIR, Config, ProductionConfig, TestingConfig

from .extensions import csrf, db, login_manager

load_dotenv(os.path.join(BASE_DIR, ".env"))

CONFIGS = {
    "default": Config,
    "development": Config,
    "production": ProductionConfig,
    "testing": TestingConfig,
}

BLUEPRINTS = (
    "auth",
    "dashboard",
    "products",
    "catalog",
    "warehouses",
    "stock",
    "purchasing",
    "sales",
    "reports",
    "users",
    "settings",
    "api",
    "scan",
    "i18n",
)


def create_app(config_name=None):
    app = Flask(__name__, instance_relative_config=False)

    config_name = config_name or os.environ.get("FLASK_CONFIG", "default")
    app.config.from_object(CONFIGS.get(config_name, Config))

    csrf.init_app(app)
    db.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  side effect: registers mappers and the user loader
    from .i18n import (
        available_locales,
        current_locale,
        money,
        number,
        short_date,
        stamp,
        translate,
    )

    app.jinja_env.globals["_"] = translate
    app.jinja_env.globals["t"] = translate
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["qty"] = number
    app.jinja_env.filters["num"] = number
    app.jinja_env.filters["dt"] = stamp
    app.jinja_env.filters["datestr"] = short_date
    app.jinja_env.filters["pct"] = lambda v: f"{float(v or 0):.1f}%"

    for name in BLUEPRINTS:
        module = __import__(f"inventory.blueprints.{name}", fromlist=["bp"])
        app.register_blueprint(module.bp)

    @app.before_request
    def load_locale_and_user():
        g.locale = current_locale()
        g.inventory_user = current_user if current_user.is_authenticated else None
        session.setdefault("locale", g.locale)

    @app.context_processor
    def inject_globals():
        from .utils import company_name, currency_symbol

        return {
            "company": company_name(),
            "currency": currency_symbol(),
            "today": date.today(),
            "locales": available_locales(),
            "active_locale": g.get("locale", "en"),
            "is_admin": current_user.is_authenticated and current_user.is_admin,
            "can_manage": current_user.is_authenticated and current_user.can_manage,
            "can_delete": current_user.is_authenticated and current_user.can_delete,
            "batch_warning_days": _expiry_days(),
            "nav_active": request.endpoint or "",
        }

    _register_security_headers(app)
    _register_cli(app)
    _register_error_handlers(app)
    return app


def _register_security_headers(app):
    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(self)"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src {app.config['CSP_SCRIPT_SRC']}; "
            f"style-src {app.config['CSP_STYLE_SRC']}; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "form-action 'self'",
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _expiry_days():
    from .services import expiry_warning_days

    return expiry_warning_days()


def _register_cli(app):
    import click

    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        from .services import init_settings

        init_settings()
        click.echo("Schema created.")

    @app.cli.command("seed")
    @click.option("--demo/--no-demo", default=True, help="Include the sample records.")
    def seed(demo):
        from .seed import run_seed

        run_seed(demo=demo)

    @app.cli.command("check-translations")
    def check_translations():
        from .i18n import LOCALES, missing_keys

        failed = False
        for code in LOCALES:
            missing = missing_keys(code)
            if missing:
                failed = True
                click.echo(f"{code}: {len(missing)} missing")
                for key in missing[:10]:
                    click.echo(f"    {key}")
            else:
                click.echo(f"{code}: complete")
        raise SystemExit(1 if failed else 0)

    @app.cli.command("reset-db")
    @click.confirmation_option(prompt="Drop every table and rebuild? All data is lost.")
    def reset_db():
        db.drop_all()
        db.create_all()
        from .services import init_settings

        init_settings()
        click.echo("Database reset.")

    @app.cli.command("routes-list")
    def routes_list():
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
            click.echo(f"{str(rule):<52} {sorted(rule.methods - {'HEAD', 'OPTIONS'})}")


def _register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "errors/error.html",
            code=403,
            message=_("You do not have permission to view that page."),
        ), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template(
            "errors/error.html",
            code=404,
            message=_("That page does not exist."),
        ), 404

    @app.errorhandler(413)
    def too_large(_error):
        limit = round(app.config.get("MAX_CONTENT_LENGTH", 0) / (1024 * 1024)) or 5
        return render_template(
            "errors/error.html",
            code=413,
            message=_("That upload is larger than {limit} MB.").format(limit=limit),
        ), 413

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled error: %s", error)
        return render_template(
            "errors/error.html",
            code=500,
            message=_("Something went wrong on our side."),
        ), 500

