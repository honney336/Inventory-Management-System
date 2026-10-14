"""Locale resolution and string translation.

Source strings are English and the key is the English text, the same approach
gettext takes. An entry missing from a locale falls back to English, so a partly
finished translation degrades to a readable interface rather than raw keys.
"""

from flask import current_app, g, has_app_context, session

from .catalog import CATALOGS, DEFAULT_LOCALE, LOCALES
from .format import format_date, format_datetime, format_money, format_number


def available_locales():
    return dict(LOCALES)


def is_supported(code):
    return bool(code) and code in LOCALES


def resolve_locale(candidates):
    for code in candidates:
        if not code:
            continue
        code = str(code).strip().replace("_", "-")
        if code in LOCALES:
            return code
        lowered = code.lower()
        for known in LOCALES:
            if known.lower() == lowered:
                return known
        primary = lowered.split("-")[0]
        if primary in LOCALES:
            return primary
    return None


def current_locale():
    stored = session.get("locale") if has_request_context() else None
    resolved = resolve_locale([stored])
    if resolved:
        return resolved

    if has_app_context() and not has_request_context():
        return DEFAULT_LOCALE

    user = getattr(g, "inventory_user", None)
    resolved = resolve_locale([getattr(user, "locale", None)])
    if resolved:
        return resolved

    from ..services import get_setting

    resolved = resolve_locale([get_setting("default_locale", DEFAULT_LOCALE)])
    if resolved:
        return resolved

    accepted = current_app.config.get("DEFAULT_LOCALE") or ""
    resolved = resolve_locale([part.split(";")[0] for part in accepted.split(",")])
    return resolved or DEFAULT_LOCALE


def has_request_context():
    from flask import has_request_context as flask_has_request_context

    return flask_has_request_context()


def translate(message, **params):
    locale = current_locale()
    entry = CATALOGS.get(locale, {}).get(message)
    if entry is None:
        entry = CATALOGS[DEFAULT_LOCALE].get(message, message)
    if params:
        try:
            return entry.format(**params)
        except (KeyError, IndexError, ValueError):
            return entry
    return entry


def money(value, symbol=None):
    from ..services import get_setting

    return format_money(value, current_locale(), symbol or get_setting("currency_symbol", "$"))


def number(value):
    return format_number(value, current_locale())


def short_date(value):
    return format_date(value, current_locale())


def stamp(value):
    return format_datetime(value, current_locale())


def missing_keys(locale):
    """Keys present in English but absent from another locale."""
    return sorted(set(CATALOGS[DEFAULT_LOCALE]) - set(CATALOGS.get(locale, {})))