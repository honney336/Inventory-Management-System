"""Sign in, sign out, first-run setup and password change."""

from datetime import timedelta

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..i18n import is_supported, translate as _
from ..models import ROLE_ADMIN, AuditLog, LoginAttempt, Setting, User, utcnow
from ..services import client_ip, log_action
from ..utils import clean, form_bool, safe_redirect

bp = Blueprint("auth", __name__, url_prefix="/auth")

MIN_PASSWORD_LENGTH = 8

DUMMY_HASH = generate_password_hash("not-a-real-password-for-timing-only")


def _lockout_state(identity, ip):
    """Return (locked, seconds remaining) for this identity and address."""
    from flask import current_app

    limit = current_app.config.get("LOGIN_MAX_ATTEMPTS", 5)
    minutes = current_app.config.get("LOGIN_LOCKOUT_MINUTES", 15)
    if limit <= 0 or minutes <= 0:
        return False, 0

    since = utcnow() - timedelta(minutes=minutes)
    rows = db.session.execute(
        db.select(LoginAttempt).where(
            LoginAttempt.attempted_at >= since,
            LoginAttempt.successful.is_(False),
            db.or_(
                LoginAttempt.identity == identity,
                LoginAttempt.ip_address == ip,
            ),
        )
    ).scalars().all()

    if len(rows) < limit:
        return False, 0

    latest = max(row.attempted_at for row in rows)
    unlock_at = latest + timedelta(minutes=minutes)
    remaining = int((unlock_at - utcnow()).total_seconds())
    return True, max(remaining, 1)


def _record_attempt(identity, ip, success):
    db.session.add(
        LoginAttempt(identity=identity[:180], ip_address=ip[:45], successful=success)
    )
    db.session.commit()


def _password_problem(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.isdigit() or password.isalpha():
        return "Password must mix letters with numbers or symbols."
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    # First run: no users exist yet, so offer the setup wizard instead of a
    # login form nobody can get past.
    user_count = db.session.execute(db.select(func.count(User.id))).scalar_one()
    if user_count == 0:
        return redirect(url_for("auth.setup"))

    if request.method == "POST":
        identity = clean(request.form, "identity", 180).lower()
        password = request.form.get("password") or ""
        remember = form_bool(request.form, "remember")
        ip = client_ip() or "unknown"

        locked, wait_seconds = _lockout_state(identity, ip)
        if locked:
            flash(
                _("Too many failed sign-ins. Try again in {minutes} minute(s).").format(
                    minutes=max(1, round(wait_seconds / 60))
                ),
                "danger",
            )
            return render_template("auth/login.html", title=_("Sign in"))

        user = db.session.execute(
            db.select(User).where(
                db.or_(User.username == identity, func.lower(User.email) == identity)
            )
        ).scalar_one_or_none()

        if user is None:
            check_password_hash(DUMMY_HASH, password)
            _record_attempt(identity, ip, success=False)
            flash(_("Those credentials do not match our records."), "danger")
        elif not user.check_password(password):
            _record_attempt(identity, ip, success=False)
            flash(_("Those credentials do not match our records."), "danger")
        elif not user.is_active:
            _record_attempt(identity, ip, success=False)
            flash(_("That account has been deactivated. Contact an administrator."), "warning")
        else:
            _record_attempt(identity, ip, success=True)
            session.clear()
            login_user(user, remember=remember)
            user.last_login = utcnow()
            log_action("auth.login", "user", user.id, user.username)
            db.session.commit()
            flash(_("Welcome back, {name}.").format(name=user.full_name or user.username), "success")
            return redirect(safe_redirect(request.args.get("next"), "dashboard.index"))

    return render_template("auth/login.html", title="Sign in")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first administrator. Permanently closed once one exists."""
    if db.session.execute(db.select(func.count(User.id))).scalar_one() > 0:
        return render_template(
            "auth/closed.html",
            title=_("Setup is closed"),
            message=_("Setup is already complete. Sign in instead."),
        ), 403

    if request.method == "POST":
        username = clean(request.form, "username", 80)
        email = clean(request.form, "email", 180).lower()
        full_name = clean(request.form, "full_name", 120)
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        errors = []
        if not username:
            errors.append("Username is required.")
        elif not db.session.execute(
            db.select(User).where(func.lower(User.username) == username.lower())
        ).scalar_one_or_none() is None:
            errors.append("That username is taken.")
        if not email:
            errors.append("Email address is required.")
        problem = _password_problem(password)
        if problem:
            errors.append(problem)
        if password != confirm:
            errors.append("The two passwords do not match.")

        if errors:
            for message in errors:
                flash(message, "danger")
        else:
            user = User(
                username=username, email=email, full_name=full_name or username, role=ROLE_ADMIN
            )
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.flush()
                db.session.add(Setting(key="installed_at", value=utcnow().isoformat()))
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return render_template(
                    "auth/closed.html",
                    title=_("Setup is closed"),
                    message=_("Setup is already complete. Sign in instead."),
                ), 403
            log_action("auth.setup", "user", user.id, username)
            flash(_("Administrator created. You can sign in now."), "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/setup.html", title="First run setup")


@bp.post("/logout")
@login_required
def logout():
    log_action("auth.logout", "user", current_user.id, current_user.username)
    db.session.commit()
    logout_user()
    flash(_("Signed out."), "info")
    return redirect(url_for("auth.login"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        full_name = clean(request.form, "full_name", 120)
        email = clean(request.form, "email", 180).lower()
        clash = db.session.execute(
            db.select(User).where(func.lower(User.email) == email, User.id != current_user.id)
        ).scalar_one_or_none()
        if not email:
            flash(_("Email address is required."), "danger")
        elif clash is not None:
            flash(_("Another account already uses that email address."), "danger")
        else:
            locale = clean(request.form, "locale", 8)
            if not is_supported(locale):
                locale = current_user.locale
            current_user.full_name = full_name
            current_user.email = email
            current_user.locale = locale
            if locale:
                session["locale"] = locale
            db.session.commit()
            flash(_("Profile updated."), "success")
            return redirect(url_for("auth.profile"))

    recent = db.session.execute(
        db.select(AuditLog)
        .where(AuditLog.user_id == current_user.id)
        .order_by(AuditLog.created_at.desc())
        .limit(10)
    ).scalars().all()

    return render_template(
        "auth/profile.html",
        title="My profile",
        user=current_user,
        recent=recent,
    )


@bp.post("/change-password")
@login_required
def change_password():
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not current_user.check_password(current):
        flash(_("Your current password is incorrect."), "danger")
    else:
        problem = _password_problem(new)
        if problem:
            flash(problem, "danger")
        elif new != confirm:
            flash(_("The two new passwords do not match."), "danger")
        else:
            current_user.set_password(new)
            log_action("auth.password_change", "user", current_user.id, current_user.username)
            db.session.commit()
            flash(_("Password changed."), "success")

    return redirect(url_for("auth.profile"))