"""Reusable route guards."""

from functools import wraps

from flask import abort, flash, redirect, request, url_for

from .models import ROLE_ADMIN, ROLE_MANAGER


def roles_required(*roles):
    """Allow the request only if the signed-in user holds one of `roles`."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from flask_login import current_user

            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.full_path))
            if roles and current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


admin_required = roles_required(ROLE_ADMIN)
manager_required = roles_required(ROLE_ADMIN, ROLE_MANAGER)


def staff_or_above(fn):
    """Any signed-in employee; used where viewers and writers share a page."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        from flask_login import current_user

        if not current_user.is_authenticated:
            flash("Sign in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)

    return wrapper