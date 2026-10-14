from datetime import date, datetime

FORMATTERS = {
    "en": {"group": ",", "decimal": ".", "date": "%d %b %Y", "datetime": "%d %b %Y %H:%M"},
    "fi": {"group": "\u00a0", "decimal": ",", "date": "%d.%m.%Y", "datetime": "%d.%m.%Y %H:%M"},
    "zh": {"group": ",", "decimal": ".", "date": "%Y-%m-%d", "datetime": "%Y-%m-%d %H:%M"},
    "ja": {"group": ",", "decimal": ".", "date": "%Y/%m/%d", "datetime": "%Y/%m/%d %H:%M"},
}

FALLBACK = FORMATTERS["en"]


def _settings(locale):
    return FORMATTERS.get(locale, FALLBACK)


def format_number(value, locale="en", decimals=None):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value)

    if decimals is None:
        decimals = 0 if float(number).is_integer() else 2

    fmt = _settings(locale)
    negative = number < 0
    fixed = f"{abs(number):,.{decimals}f}"
    if fmt["group"] == ",":
        whole, _, fraction = fixed.partition(".")
    else:
        whole, _, fraction = fixed.replace(",", "@").partition(".")
        whole = whole.replace("@", fmt["group"])

    if fmt["decimal"] != ".":
        fraction = fraction.replace(".", fmt["decimal"])

    out = whole + (fmt["decimal"] + fraction if fraction else "")
    return ("-" + out) if negative else out


def format_money(value, locale="en", symbol="$", decimals=2):
    amount = format_number(value, locale, decimals=decimals)
    return f"{symbol}{amount}"


def format_date(value, locale="en", empty="—"):
    if not value:
        return empty
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return str(value)
    return value.strftime(_settings(locale)["date"])


def format_datetime(value, locale="en", empty="—"):
    if not value:
        return empty
    if isinstance(value, str):
        return value
    if not isinstance(value, (datetime, date)):
        return str(value)
    return value.strftime(_settings(locale)["datetime"])