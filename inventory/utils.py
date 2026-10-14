"""Helpers shared across blueprints: parsing, formatting, pagination, exports."""

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import request, url_for
from sqlalchemy import or_

from .extensions import db


def ean13_check_digit(first_twelve):
    """The digit that makes a twelve digit body a valid EAN-13."""
    total = 0
    for index, char in enumerate(str(first_twelve)[:12]):
        total += int(char) * (3 if index % 2 else 1)
    return (10 - total % 10) % 10


def barcode_variants(code):
    """Every form a scanned code might legitimately arrive in.

    A label always prints the full thirteen digit EAN because that is what the
    bars encode, so a reader returns thirteen digits even when the value typed
    into the product form had twelve. People also type and print codes with
    spaces or dashes in them. Normalising here means a label printed by this
    app resolves back to the product it came from.
    """
    raw = str(code or "").strip()
    if not raw:
        return []

    variants = [raw]
    spaced = "".join(char for char in raw if not char.isspace())
    if spaced and spaced not in variants:
        variants.append(spaced)

    # only strip separators out of something that is numeric to begin with,
    # so an alphanumeric code never turns into a stray run of digits
    digits = "".join(char for char in spaced if char.isdigit())
    numeric = digits and all(char.isdigit() or char in "-./ " for char in spaced)
    if numeric and digits not in variants:
        variants.append(digits)

    if len(digits) == 12:
        # a reader may hand back the twelve digits that were typed, the full
        # thirteen digit EAN the label encodes, or the zero padded form
        for candidate in (digits + str(ean13_check_digit(digits)), "0" + digits):
            if candidate not in variants:
                variants.append(candidate)
    elif len(digits) == 13:
        if digits[-1] == str(ean13_check_digit(digits)):
            without_check = digits[:12]
            if without_check not in variants:
                variants.append(without_check)
        if digits[0] == "0" and digits[1:] not in variants:
            variants.append(digits[1:])
    return variants


def barcode_matches(column, code):
    """A SQL condition matching a barcode column against any variant of a code."""
    variants = barcode_variants(code)
    if not variants:
        return column.is_(None)
    return column.in_(variants)


def to_int(value, default=0, minimum=None):
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    if minimum is not None and result < minimum:
        return minimum
    return result


def to_decimal(value, default=Decimal("0")):
    try:
        cleaned = str(value).strip().replace(",", "").replace(" ", "")
        if cleaned in {"", "-", ".", "n/a", "N/A"}:
            return default
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return default


def to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def form_bool(form, field, default=False):
    raw = form.get(field)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "checked"}


def clean(form, field, max_length=None, default=""):
    value = (form.get(field) or default).strip()
    if max_length and len(value) > max_length:
        value = value[:max_length]
    return value


def is_safe_url(target):
    """Only allow redirects that stay on this host."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(target)
    return test_url.scheme in {"http", "https"} and ref_url.netloc == test_url.netloc


def safe_redirect(target, fallback_endpoint="dashboard.index"):
    if is_safe_url(target):
        return target
    return url_for(fallback_endpoint)


def money(value, symbol="$"):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{symbol}{amount:,.2f}"


def qty(value):
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{number:,}"


def short_datetime(value):
    if not value:
        return "—"
    if isinstance(value, str):
        return value
    return value.strftime("%d %b %Y, %H:%M")


def setting(key, default=""):
    from .models import Setting

    row = db.session.get(Setting, key)
    if row is None or row.value is None:
        return default
    return row.value


def currency_symbol():
    return setting("currency_symbol", "$")


def company_name():
    return setting("company_name", "Inventory System")


def search_term():
    return (request.args.get("q") or "").strip()


def paginate(query, per_page=None, endpoint=None, **kwargs):
    """Turn a SQLAlchemy 2.x select into items plus a pagination dict.

    Pagination links keep the current query string, so filters survive paging.
    """
    from flask import current_app

    per_page = per_page or current_app.config["ITEMS_PER_PAGE"]
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    total = db.session.execute(
        db.select(db.func.count()).select_from(query.order_by(None).subquery())
    ).scalar_one()
    total = int(total or 0)

    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    items = db.session.execute(query.limit(per_page).offset((page - 1) * per_page)).scalars().all()
    return {
        "items": items,
        "pagination": {
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "total": total,
            "has_prev": page > 1,
            "has_next": page < pages,
            "prev_num": page - 1,
            "next_num": page + 1,
        },
        "endpoint": endpoint,
        "kwargs": kwargs,
    }


def page_link(endpoint, page, **kwargs):
    args = dict(request.args.to_dict())
    args.update(kwargs)
    args["page"] = page
    return url_for(endpoint, **args)


def like(column, term):
    return column.ilike(f"%{term}%")


def any_search(columns, term):
    return or_(*[like(col, term) for col in columns])


def sort_args(default_key, allowed):
    """Read ?sort=field&dir=asc but only allow known columns."""
    key = request.args.get("sort", default_key)
    if key not in allowed:
        key = default_key
    direction = request.args.get("dir", "asc").lower()
    if direction not in {"asc", "desc"}:
        direction = "asc"
    return key, direction


def _spreadsheet_safe(value):
    """Stop a cell being executed as a formula when the file is opened."""
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def csv_response(filename, headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_spreadsheet_safe(cell) for cell in row])
    return buffer.getvalue(), filename


def parse_upload(file_storage):
    """Return (rows, errors) from an uploaded CSV file."""
    errors = []
    rows = []
    if file_storage is None or not file_storage.filename:
        return rows, ["No file selected."]

    raw = file_storage.read()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raw = raw.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        return rows, ["The CSV file appears to be empty."]

    normalised = {name.strip().lower(): name for name in reader.fieldnames}
    required = {"sku", "name"}
    missing = required - set(normalised)
    if missing:
        return rows, [f"Missing required column(s): {', '.join(sorted(missing))}"]

    for lineno, raw_row in enumerate(reader, start=2):
        if all((value or "").strip() == "" for value in raw_row.values()):
            continue
        row = {}
        for lowered, original in normalised.items():
            row[lowered] = (raw_row.get(original) or "").strip()
        row["_line"] = lineno
        if not row.get("sku") or not row.get("name"):
            errors.append(f"Line {lineno}: sku and name are required.")
            continue
        rows.append(row)

    return rows, errors