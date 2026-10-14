from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from ..decorators import admin_required
from ..extensions import db
from ..i18n import LOCALES, translate as _
from ..models import AuditLog
from ..notifier import UnsafeURLError, validate_webhook_url
from ..services import DEFAULT_SETTINGS, get_setting, log_action, set_setting
from ..utils import clean, paginate, to_int

bp = Blueprint("settings", __name__, url_prefix="/settings")

TEXT_SETTINGS = {
    "company_name": "Organisation name",
    "currency_symbol": "Currency symbol",
}

NUMBER_SETTINGS = {
    "low_stock_threshold": "Default low stock threshold",
    "default_lead_time_days": "Default supplier lead time (days)",
    "service_level": "Service level target (%)",
    "review_period_days": "Stock review period (days)",
    "expiry_warning_days": "Expiry warning window (days)",
}

TOGGLE_SETTINGS = {
    "allow_negative_stock": "Allow stock to go negative",
}

URL_SETTINGS = {
    "notify_webhook_url": "Notification webhook URL",
}

SELECT_SETTINGS = {
    "default_locale": "Default language",
}


@bp.get("")
@admin_required
def index():
    return render_template(
        "settings/index.html",
        title=_("Settings"),
        values={key: get_setting(key) for key in DEFAULT_SETTINGS},
        text_settings=TEXT_SETTINGS,
        number_settings=NUMBER_SETTINGS,
        toggle_settings=TOGGLE_SETTINGS,
        url_settings=URL_SETTINGS,
        select_settings=SELECT_SETTINGS,
        locales=LOCALES,
    )


@bp.post("")
@admin_required
def update():
    for key in TEXT_SETTINGS:
        set_setting(key, clean(request.form, key, 120) or DEFAULT_SETTINGS.get(key, ""))

    for key in NUMBER_SETTINGS:
        set_setting(key, str(to_int(request.form.get(key), to_int(DEFAULT_SETTINGS[key], 0))))

    for key in TOGGLE_SETTINGS:
        value = request.form.get(key, "").strip().lower()
        set_setting(key, "true" if value in {"1", "on", "true", "yes"} else "false")

    for key in SELECT_SETTINGS:
        value = request.form.get(key, "")
        set_setting(key, value if value in LOCALES else DEFAULT_SETTINGS["default_locale"])

    for key, label in URL_SETTINGS.items():
        value = clean(request.form, key, 300)
        try:
            set_setting(key, validate_webhook_url(value))
        except UnsafeURLError as exc:
            flash(_("{field}: {error}").format(field=label, error=str(exc)), "danger")

    log_action("settings.update", "settings", None, ", ".join(sorted(request.form.keys())))
    db.session.commit()
    flash(_("Settings saved."), "success")
    return redirect(url_for("settings.index"))


@bp.get("/audit")
@admin_required
def audit():
    term = (request.args.get("q") or "").strip()
    action = request.args.get("action", "")
    user_id = request.args.get("user", type=int)

    query = db.select(AuditLog)
    if term:
        query = query.where(
            db.or_(
                AuditLog.details.ilike(f"%{term}%"),
                AuditLog.action.ilike(f"%{term}%"),
                AuditLog.entity.ilike(f"%{term}%"),
            )
        )
    if action:
        query = query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)

    result = paginate(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()),
        per_page=40,
    )

    actions = db.session.execute(
        db.select(AuditLog.action).distinct().order_by(AuditLog.action)
    ).scalars().all()

    return render_template(
        "settings/audit.html",
        title=_("Activity log"),
        rows=result["items"],
        pagination=result["pagination"],
        actions=actions,
        term=term,
        selected_action=action,
        user_id=user_id,
    )


@bp.get("/about")
@login_required
def about():
    from flask import current_app

    return render_template(
        "settings/about.html",
        title=_("System information"),
        routes=sorted(current_app.url_map.iter_rules(), key=lambda r: str(r)),
        models=sorted(db.metadata.tables.keys()),
        version=current_app.config.get("APP_VERSION", "1.0"),
    )