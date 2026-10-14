"""User administration. Every route here is admin-only."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..decorators import admin_required
from ..i18n import LOCALES, translate as _
from ..extensions import db
from ..models import ROLES, ROLE_ADMIN, ROLE_STAFF, User
from ..services import log_action
from ..utils import clean, form_bool, paginate

bp = Blueprint("users", __name__, url_prefix="/users")

MIN_PASSWORD_LENGTH = 8


@bp.get("")
@admin_required
def index():
    term = (request.args.get("q") or "").strip()
    query = db.select(User)
    if term:
        query = query.where(
            or_(User.username.ilike(f"%{term}%"), User.email.ilike(f"%{term}%"))
        )
    result = paginate(query.order_by(User.role, User.username), endpoint="users.index")

    return render_template(
        "users/index.html",
        title="Users",
        users=result["items"],
        pagination=result["pagination"],
        term=term,
        roles=ROLES,
    )


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    if request.method == "POST":
        username = clean(request.form, "username", 80)
        email = clean(request.form, "email", 180).lower()
        role = clean(request.form, "role", 20) or ROLE_STAFF
        password = request.form.get("password") or ""

        errors = []
        if not username:
            errors.append("Username is required.")
        if not email:
            errors.append("Email address is required.")
        if role not in ROLES:
            errors.append("Pick a valid role.")
        if len(password) < MIN_PASSWORD_LENGTH:
            errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

        if not errors:
            requested_locale = clean(request.form, "locale", 8)
            user = User(
                username=username,
                email=email,
                full_name=clean(request.form, "full_name", 120),
                role=role,
                locale=requested_locale if requested_locale in LOCALES else None,
            )
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.flush()
                log_action("user.create", "user", user.id, f"{username} ({role})")
                db.session.commit()
                flash(_("User '{name}' created.").format(name=username), "success")
                return redirect(url_for("users.index"))
            except IntegrityError:
                db.session.rollback()
                errors.append("That username or email is already registered.")

        for message in errors:
            flash(message, "danger")

    return render_template("users/form.html", title="New user", user=None, roles=ROLES)


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(user_id):
    user = db.get_or_404(User, user_id)

    if request.method == "POST":
        email = clean(request.form, "email", 180).lower()
        role = clean(request.form, "role", 20) or ROLE_STAFF
        locale = clean(request.form, "locale", 8) or None
        errors = []

        if not email:
            errors.append("Email address is required.")
        if role not in ROLES:
            errors.append("Pick a valid role.")
        if user.id == current_user.id and role != ROLE_ADMIN:
            errors.append("You cannot remove your own administrator role.")
        if not form_bool(request.form, "is_active", True) and user.id == current_user.id:
            errors.append("You cannot deactivate your own account.")

        clash = db.session.execute(
            db.select(User).where(func.lower(User.email) == email, User.id != user.id)
        ).scalar_one_or_none()

        if not errors and clash is not None:
            errors.append("Another account already uses that email address.")

        if not errors:
            user.email = email
            user.full_name = clean(request.form, "full_name", 120)
            user.role = role
            user.locale = locale if locale in LOCALES else None
            user.is_active_flag = form_bool(request.form, "is_active", True)
            try:
                log_action("user.update", "user", user.id, f"{user.username} ({user.role})")
                db.session.commit()
                flash(_("User updated."), "success")
                return redirect(url_for("users.index"))
            except IntegrityError:
                db.session.rollback()
                errors.append("That email address is already registered.")

        for message in errors:
            flash(message, "danger")

    return render_template("users/form.html", title=f"Edit {user.username}",
                           user=user, roles=ROLES)


@bp.post("/<int:user_id>/password")
@admin_required
def reset_password(user_id):
    user = db.get_or_404(User, user_id)
    password = request.form.get("new_password") or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(_("Password must be at least {count} characters.").format(count=MIN_PASSWORD_LENGTH), "danger")
    else:
        user.set_password(password)
        log_action("user.password_reset", "user", user.id, user.username)
        db.session.commit()
        flash(_("Password reset for '{name}'.").format(name=user.username), "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:user_id>/toggle")
@admin_required
def toggle_active(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash(_("You cannot deactivate your own account."), "warning")
    else:
        user.is_active_flag = not user.is_active
        log_action("user.toggle", "user", user.id, f"{user.username} active={user.is_active}")
        db.session.commit()
        flash(_("{name} is now {state}.").format(name=user.username, state=_("active") if user.is_active else _("inactive")), "success")
    return redirect(url_for("users.index"))


@bp.post("/<int:user_id>/delete")
@admin_required
def delete(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash(_("You cannot delete your own account."), "warning")
        return redirect(url_for("users.index"))

    remaining = db.session.execute(
        db.select(func.count(User.id)).where(User.role == ROLE_ADMIN, User.id != user.id)
    ).scalar_one()
    if user.role == ROLE_ADMIN and remaining == 0:
        flash(_("The last administrator cannot be deleted."), "warning")
        return redirect(url_for("users.index"))

    username = user.username
    log_action("user.delete", "user", user.id, username)
    db.session.delete(user)
    db.session.commit()
    flash(_("User '{name}' deleted.").format(name=username), "success")
    return redirect(url_for("users.index"))