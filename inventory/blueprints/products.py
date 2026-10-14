"""Product catalogue: CRUD, search, bulk import/export and printable labels."""

from datetime import date

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..decorators import admin_required, manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import (
    Batch,
    Category,
    Product,
    SalesOrderLine,
    StockMovement,
    Supplier,
    Warehouse,
)
from ..notifier import check_expiry, check_stock_levels
from ..services import (
    StockError,
    adjust_stock,
    expiry_warning_days,
    log_action,
    reorder_recommendation,
)
from ..utils import (
    barcode_matches,
    clean,
    csv_response,
    form_bool,
    paginate,
    parse_upload,
    to_decimal,
    to_int,
)

bp = Blueprint("products", __name__, url_prefix="/products")

SORTABLE = {"sku", "name", "category", "cost", "price", "stock", "updated_at"}


@bp.get("")
@login_required
def index():
    term = (request.args.get("q") or "").strip()
    category_id = request.args.get("category", type=int)
    supplier_id = request.args.get("supplier", type=int)
    status = request.args.get("status", "all")
    stock = request.args.get("stock", "all")

    query = db.select(Product).outerjoin(Category, Product.category_id == Category.id)

    if term:
        query = query.where(
            or_(
                Product.name.ilike(f"%{term}%"),
                Product.sku.ilike(f"%{term}%"),
                Product.barcode == term,
                Product.description.ilike(f"%{term}%"),
                Category.name.ilike(f"%{term}%"),
            )
        )
    if category_id:
        query = query.where(Product.category_id == category_id)
    if supplier_id:
        query = query.where(Product.supplier_id == supplier_id)
    if status in {"active", "inactive"}:
        query = query.where(Product.is_active_flag.is_(status == "active"))

    sort = request.args.get("sort", "name")
    direction = request.args.get("dir", "asc")
    column = {
        "sku": Product.sku,
        "name": Product.name,
        "category": Category.name,
        "cost": Product.cost,
        "price": Product.price,
        "updated_at": Product.updated_at,
    }.get(sort, Product.name)
    query = query.order_by(column.desc() if direction == "desc" else column.asc())

    result = paginate(query, endpoint="products.index")
    rows = result["items"]

    # Stock level filters run after materialisation because the on-hand figure
    # is the sum across warehouses.
    if stock == "low":
        rows = [p for p in rows if p.stock_status == "low"]
    elif stock == "out":
        rows = [p for p in rows if p.stock_status == "out"]
    elif stock == "in":
        rows = [p for p in rows if p.quantity > 0]

    return render_template(
        "products/index.html",
        title="Products",
        products=rows,
        pagination=result["pagination"],
        categories=db.session.execute(
            db.select(Category).order_by(Category.name)
        ).scalars().all(),
        suppliers=db.session.execute(
            db.select(Supplier).order_by(Supplier.name)
        ).scalars().all(),
        term=term,
        status=status,
        stock_filter=stock,
        category_id=category_id,
        supplier_id=supplier_id,
        sort=sort,
        direction=direction,
    )


def _read_product_form(form):
    """Validate the shared product fields. Returns (data, errors)."""
    errors = []
    data = {
        "sku": clean(form, "sku", 60).upper(),
        "barcode": clean(form, "barcode", 60),
        "name": clean(form, "name", 200),
        "description": clean(form, "description"),
        "category_id": to_int(form.get("category_id"), 0) or None,
        "supplier_id": to_int(form.get("supplier_id"), 0) or None,
        "unit": clean(form, "unit", 24) or "each",
        "cost": to_decimal(form.get("cost")),
        "price": to_decimal(form.get("price")),
        "tax_rate": to_decimal(form.get("tax_rate")),
        "reorder_point": to_int(form.get("reorder_point"), 0, minimum=0),
        "reorder_quantity": to_int(form.get("reorder_quantity"), 1, minimum=1),
        "location_bin": clean(form, "location_bin", 40),
        "is_active_flag": form_bool(form, "is_active", default=True),
        "track_batches": form_bool(form, "track_batches", default=False),
        "shelf_life_days": to_int(form.get("shelf_life_days"), 0) or None,
    }

    if not data["sku"]:
        errors.append("SKU is required.")
    elif not all(ch.isalnum() or ch in "-_." for ch in data["sku"]):
        errors.append("SKU may only contain letters, numbers, dots, dashes and underscores.")
    if not data["name"]:
        errors.append("Product name is required.")
    if data["price"] < 0 or data["cost"] < 0:
        errors.append("Cost and price cannot be negative.")

    return data, errors


@bp.route("/new", methods=["GET", "POST"])
@manager_required
def create():
    if request.method == "POST":
        data, errors = _read_product_form(request.form)
        if not errors:
            conditions = [Product.sku == data["sku"]]
            if data["barcode"]:
                conditions.append(barcode_matches(Product.barcode, data["barcode"]))
            duplicate = db.session.execute(
                db.select(Product).where(or_(*conditions))
            ).scalars().first()
            if duplicate is not None:
                errors.append(f"SKU or barcode already used by '{duplicate.name}'.")

        if not errors:
            product = Product(**data)
            product.barcode = data["barcode"] or None
            db.session.add(product)
            try:
                db.session.flush()
                log_action("product.create", "product", product.id, f"{product.sku} {product.name}")
                db.session.commit()
                flash(_("Product '{name}' created.").format(name=product.name), "success")
                return redirect(url_for("products.detail", product_id=product.id))
            except IntegrityError:
                db.session.rollback()
                errors.append("That SKU is already in use.")
        for message in errors:
            flash(message, "danger")

    return render_template(
        "products/form.html",
        title="New product",
        product=None,
        categories=_categories(),
        suppliers=_suppliers(),
        warehouses=_warehouses(),
        form_data=request.form,
    )


@bp.route("/<int:product_id>")
@login_required
def detail(product_id):
    product = db.get_or_404(Product, product_id)

    movements = db.session.execute(
        db.select(StockMovement)
        .where(StockMovement.product_id == product.id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(50)
    ).scalars().all()

    per_warehouse = [
        (item.warehouse, item.quantity, float(item.quantity) * float(product.cost or 0))
        for item in product.stock_items
        if item.warehouse
    ]

    inbound = db.session.execute(
        db.select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.product_id == product.id, StockMovement.quantity > 0
        )
    ).scalar_one()
    outbound = db.session.execute(
        db.select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.product_id == product.id, StockMovement.quantity < 0
        )
    ).scalar_one()

    batches = list(product.batches.filter(Batch.quantity > 0).order_by(
        Batch.expiry_date.is_(None), Batch.expiry_date, Batch.batch_no
    ))

    return render_template(
        "products/detail.html",
        title=product.name,
        product=product,
        movements=movements,
        per_warehouse=per_warehouse,
        inbound=int(inbound or 0),
        outbound=abs(int(outbound or 0)),
        warehouses=_warehouses(),
        batches=batches,
        warning_days=expiry_warning_days(),
        recommendation=reorder_recommendation(product),
    )


@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@manager_required
def edit(product_id):
    product = db.get_or_404(Product, product_id)

    if request.method == "POST":
        data, errors = _read_product_form(request.form)
        clash = db.session.execute(
            db.select(Product).where(
                Product.sku == data["sku"],
                Product.id != product.id,
            )
        ).scalars().first()
        if clash is not None:
            errors.append("Another product already uses that SKU.")

        if not errors and data["barcode"]:
            clash = db.session.execute(
                db.select(Product).where(
                    barcode_matches(Product.barcode, data["barcode"]),
                    Product.id != product.id,
                )
            ).scalars().first()
            if clash is not None:
                errors.append(
                    f"Another product already uses barcode {data['barcode']}."
                )

        if not errors:
            for field, value in data.items():
                setattr(product, field, value)
            product.barcode = data["barcode"] or None
            try:
                db.session.flush()
                log_action("product.update", "product", product.id, product.sku)
                db.session.commit()
                flash(_("Product updated."), "success")
                return redirect(url_for("products.detail", product_id=product.id))
            except IntegrityError:
                db.session.rollback()
                errors.append("That SKU or barcode is already in use.")
        for message in errors:
            flash(message, "danger")

    return render_template(
        "products/form.html",
        title=f"Edit {product.name}",
        product=product,
        categories=_categories(),
        suppliers=_suppliers(),
        warehouses=_warehouses(),
        form_data=request.form,
    )


@bp.post("/<int:product_id>/delete")
@admin_required
def delete(product_id):
    product = db.get_or_404(Product, product_id)
    name = product.name
    used = (
        db.session.execute(
            db.select(func.count(SalesOrderLine.id)).where(
                SalesOrderLine.product_id == product.id
            )
        ).scalar_one()
        > 0
    )
    if used:
        flash(
            _("This product appears on sales orders and cannot be deleted. "
              "Deactivate it instead so history is preserved."),
            "warning",
        )
        return redirect(url_for("products.detail", product_id=product.id))

    check_expiry()
    log_action("product.delete", "product", product.id, f"{product.sku} {name}")
    db.session.delete(product)
    db.session.commit()
    flash(_("Product '{name}' deleted.").format(name=name), "success")
    return redirect(url_for("products.index"))


@bp.post("/<int:product_id>/adjust")
@manager_required
def adjust(product_id):
    """Count sheet entry: set on-hand for a single warehouse."""
    product = db.get_or_404(Product, product_id)
    warehouse_id = to_int(request.form.get("warehouse_id"), 0)
    warehouse = db.session.execute(
        db.select(Warehouse).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()
    if warehouse is None:
        flash(_("Choose a warehouse."), "danger")
        return redirect(url_for("products.detail", product_id=product.id))

    counted = to_int(request.form.get("counted_quantity"), -1)
    if counted < 0:
        flash(_("Enter the counted quantity (zero or more)."), "danger")
        return redirect(url_for("products.detail", product_id=product.id))

    try:
        item, delta = adjust_stock(
            product, warehouse, counted,
            note=clean(request.form, "note"),
            batch_id=to_int(request.form.get("batch_id"), 0) or None,
        )
        log_action("product.adjust", "product", product.id,
                   f"{product.sku} @ {warehouse.code}: {delta:+d}")
        db.session.commit()
        check_stock_levels([product])
        check_expiry()
        flash(_("Stock adjusted by {delta:+d}.").format(delta=delta), "success")
    except StockError as exc:
        db.session.rollback()
        flash(str(exc), "danger")

    return redirect(request.referrer or url_for("products.detail", product_id=product.id))


@bp.post("/<int:product_id>/apply-recommendation")
@manager_required
def apply_recommendation(product_id):
    product = db.get_or_404(Product, product_id)
    recommendation = reorder_recommendation(product)
    if recommendation is None:
        flash(_("No issue history yet, so there is nothing to base a suggestion on."), "info")
        return redirect(url_for("products.detail", product_id=product.id))

    product.reorder_point = recommendation["reorder_point"]
    product.reorder_quantity = recommendation["reorder_quantity"]
    log_action("product.reorder_recommendation", "product", product.id,
               f"par {product.reorder_point}, qty {product.reorder_quantity}")
    db.session.commit()
    flash(
        _("Reorder point set to {point}, order quantity to {quantity}.").format(
            point=product.reorder_point, quantity=product.reorder_quantity
        ),
        "success",
    )
    return redirect(url_for("products.detail", product_id=product.id))


@bp.get("/export.csv")
@login_required
def export_csv():
    products = db.session.execute(
        db.select(Product).order_by(Product.sku)
    ).scalars().all()
    rows = []
    for p in products:
        rows.append([
            p.sku, p.barcode or "", p.name, p.category.name if p.category else "",
            p.supplier.name if p.supplier else "", p.unit, float(p.cost or 0),
            float(p.price or 0), p.quantity, p.reorder_point, p.location_bin or "",
            "yes" if p.is_active else "no", p.updated_at.strftime("%Y-%m-%d"),
        ])
    payload, filename = csv_response(
        f"products-{date.today():%Y%m%d}.csv",
        ["sku", "barcode", "name", "category", "supplier", "unit", "cost", "price",
         "on_hand", "reorder_point", "bin", "active", "last_updated"],
        rows,
    )
    return Response(payload, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/import", methods=["GET", "POST"])
@manager_required
def import_csv():
    if request.method == "POST":
        upload = request.files.get("file")
        update_existing = form_bool(request.form, "update_existing")
        rows, errors = parse_upload(upload)

        created = updated = 0
        if not errors:
            for row in rows:
                product = db.session.execute(
                    db.select(Product).where(Product.sku == row["sku"])
                ).scalar_one_or_none()
                if product is None:
                    product = Product(sku=row["sku"], name=row["name"])
                    db.session.add(product)
                    created += 1
                elif not update_existing:
                    errors.append(f"Line {row['_line']}: SKU {row['sku']} already exists, skipped.")
                    continue
                else:
                    updated += 1

                product.barcode = row.get("barcode") or None
                product.name = row["name"]
                product.description = row.get("description") or None
                product.cost = to_decimal(row.get("cost"))
                product.price = to_decimal(row.get("price"))
                product.reorder_point = to_int(row.get("reorder_point"), 0, minimum=0)
                product.reorder_quantity = to_int(row.get("reorder_quantity"), 1, minimum=1)
                product.unit = row.get("unit") or "each"
                product.location_bin = row.get("location_bin") or None

                category_name = (row.get("category") or "").strip()
                if category_name:
                    category = db.session.execute(
                        db.select(Category).where(func.lower(Category.name) == category_name.lower())
                    ).scalar_one_or_none()
                    if category is None:
                        category = Category(name=category_name)
                        db.session.add(category)
                        db.session.flush()
                    product.category = category

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors.append("Import failed: a barcode in the file is already assigned.")

            if created or updated:
                log_action("product.import", "product", None, f"+{created} ~{updated}")
                flash(_("Import finished: {created} created, {updated} updated.").format(created=created, updated=updated), "success")

        for message in errors[:20]:
            flash(message, "danger")
        if len(errors) > 20:
            flash(_("...and {count} more problems.").format(count=len(errors) - 20), "warning")

        return redirect(url_for("products.import_csv"))

    return render_template("products/import.html", title="Import products")


@bp.get("/labels")
@login_required
def labels():
    """Printable shelf labels for the products in the current filter."""
    ids = [int(i) for i in request.args.getlist("ids") if i.isdigit()]
    query = db.select(Product).where(Product.is_active_flag.is_(True))
    if ids:
        query = query.where(Product.id.in_(ids[:300]))
    else:
        query = query.order_by(Product.category_id, Product.name).limit(48)

    products = db.session.execute(query).scalars().all()
    return render_template(
        "products/labels.html",
        title="Shelf labels",
        products=products,
        copies=max(1, min(4, request.args.get("copies", 1, type=int))),
    )


@bp.get("/<int:product_id>/barcode")
@login_required
def barcode(product_id):
    product = db.get_or_404(Product, product_id)
    value = product.barcode or product.sku
    return render_template(
        "products/barcode.html",
        title=f"Barcode {value}",
        product=product,
        value=value,
    )


@bp.get("/lookup")
@login_required
def lookup():
    """Small JSON endpoint used by the scan page to resolve a barcode."""
    code = (request.args.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "No code supplied"}, 400

    product = db.session.execute(
        db.select(Product).where(
            or_(barcode_matches(Product.barcode, code),
                func.lower(Product.sku) == code.lower())
        )
    ).scalars().first()

    if product is None:
        return {"ok": False, "error": f"No product matches '{code}'"}, 404

    return {
        "ok": True,
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "on_hand": product.quantity,
        "reorder_point": product.reorder_point,
        "status": product.stock_status,
        "unit_price": float(product.price or 0),
    }


def _categories():
    return db.session.execute(db.select(Category).order_by(Category.name)).scalars().all()


def _suppliers():
    return db.session.execute(
        db.select(Supplier).where(Supplier.is_active_flag.is_(True)).order_by(Supplier.name)
    ).scalars().all()


def _warehouses():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
    ).scalars().all()