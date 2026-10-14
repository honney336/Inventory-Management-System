from flask import Blueprint, redirect, request, session, url_for
from flask_login import current_user

from ..i18n import is_supported
from ..services import log_action, set_setting
from ..i18n import translate as _
from ..extensions import db

bp = Blueprint("i18n", __name__, url_prefix="/language")


@bp.get("/<code>")
def switch(code):
    return _apply(code, request.args.get("next"))


@bp.post("/<code>")
def switch_post(code):
    return _apply(code, request.form.get("next"))


def _apply(code, next_url=None):
    if is_supported(code):
        session["locale"] = code
        session.permanent = False
        if current_user.is_authenticated and current_user.locale != code:
            current_user.locale = code
            db.session.commit()

    from ..utils import safe_redirect

    return redirect(safe_redirect(next_url, "dashboard.index"))


@bp.post("/default")
def set_default():
    code = request.form.get("locale", "")
    if is_supported(code):
        set_setting("default_locale", code)
        log_action("settings.locale", "settings", None, f"default language {code}")
        db.session.commit()
    from flask import flash

    flash(_("Default language saved."), "success")
    return redirect(url_for("settings.index"))