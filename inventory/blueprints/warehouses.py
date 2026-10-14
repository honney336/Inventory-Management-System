"""Warehouses and the stock-per-warehouse overview."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..decorators import admin_required, manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import Product, StockItem, Warehouse
from ..services import log_action
from ..utils import clean, form_bool, paginate, to_int

bp = Blueprint("warehouses", __name__, url_prefix="/warehouses")


@bp.get("")
@login_required
def index():
    term = (request.args.get("q") or "").strip()
    query = db.select(Warehouse)
    if term:
        query = query.where(Warehouse.name.ilike(f"%{term}%"))
    result = paginate(query.order_by(Warehouse.code), endpoint="warehouses.index")

    totals = {}
    for item, qty_value in db.session.execute(
        db.select(StockItem.warehouse_id, func.coalesce(func.sum(StockItem.quantity), 0))
        .group_by(StockItem.warehouse_id)
    ).all():
        totals[item] = int(qty_value or 0)

    return render_template(
        "warehouses/index.html",
        title="Warehouses",
        warehouses=result["items"],
        pagination=result["pagination"],
        totals=totals,
        term=term,
    )


@bp.get("/stock")
@login_required
def stock_grid():
    """Every product against every warehouse, for spotting concentration risk."""
    warehouse_id = request.args.get("warehouse", type=int)
    term = (request.args.get("q") or "").strip()

    query = db.select(StockItem).join(Product, StockItem.product_id == Product.id)
    if warehouse_id:
        query = query.where(StockItem.warehouse_id == warehouse_id)
    if term:
        query = query.where(
            db.or_(Product.name.ilike(f"%{term}%"), Product.sku.ilike(f"%{term}%"))
        )

    items = db.session.execute(query.order_by(Product.name).limit(800)).scalars().all()
    warehouses = db.session.execute(
        db.select(Warehouse).order_by(Warehouse.code)
    ).scalars().all()

    by_product = {}
    for item in items:
        by_product.setdefault(item.product_id, {})[item.warehouse_id] = item.quantity

    products = []
    if by_product:
        products = db.session.execute(
            db.select(Product)
            .where(Product.id.in_(list(by_product.keys())))
            .order_by(Product.name)
        ).scalars().all()

    return render_template(
        "warehouses/stock_grid.html",
        title="Stock by warehouse",
        products=products,
        matrix=by_product,
        warehouses=warehouses,
        warehouse_id=warehouse_id,
        term=term,
    )


@bp.route("/new", methods=["GET", "POST"])
@manager_required
def create():
    if request.method == "POST":
        code = clean(request.form, "code", 20).upper()
        name = clean(request.form, "name", 120)
        if not code or not name:
            flash(_("Code and name are both required."), "danger")
        else:
            warehouse = Warehouse(
                code=code,
                name=name,
                location=clean(request.form, "location", 200),
                is_active_flag=form_bool(request.form, "is_active", True),
            )
            db.session.add(warehouse)
            try:
                db.session.flush()
                log_action("warehouse.create", "warehouse", warehouse.id, code)
                db.session.commit()
                flash(_("Warehouse '{name}' created.").format(name=name), "success")
                return redirect(url_for("warehouses.index"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A warehouse with that code already exists."), "danger")

    return render_template("warehouses/form.html", title="New warehouse", warehouse=None)


@bp.route("/<int:warehouse_id>/edit", methods=["GET", "POST"])
@manager_required
def edit(warehouse_id):
    warehouse = db.get_or_404(Warehouse, warehouse_id)
    if request.method == "POST":
        code = clean(request.form, "code", 20).upper()
        name = clean(request.form, "name", 120)
        if not code or not name:
            flash(_("Code and name are both required."), "danger")
        else:
            warehouse.code = code
            warehouse.name = name
            warehouse.location = clean(request.form, "location", 200)
            warehouse.is_active_flag = form_bool(request.form, "is_active", True)
            try:
                log_action("warehouse.update", "warehouse", warehouse.id, code)
                db.session.commit()
                flash(_("Warehouse updated."), "success")
                return redirect(url_for("warehouses.index"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A warehouse with that code already exists."), "danger")

    return render_template("warehouses/form.html", title="Edit warehouse", warehouse=warehouse)


@bp.post("/<int:warehouse_id>/delete")
@admin_required
def delete(warehouse_id):
    warehouse = db.get_or_404(Warehouse, warehouse_id)
    held = db.session.execute(
        db.select(func.coalesce(func.sum(StockItem.quantity), 0)).where(
            StockItem.warehouse_id == warehouse.id
        )
    ).scalar_one()

    if held:
        warehouse.is_active_flag = False
        db.session.commit()
        flash(
            _("'{name}' still holds {count} units, so it was deactivated.").format(
                name=warehouse.name, count=int(held)
            ),
            "warning",
        )
    else:
        log_action("warehouse.delete", "warehouse", warehouse.id, warehouse.code)
        db.session.delete(warehouse)
        db.session.commit()
        flash(_("Warehouse deleted."), "success")

    return redirect(url_for("warehouses.index"))


@bp.post("/<int:warehouse_id>/quick-adjust")
@manager_required
def quick_adjust(warehouse_id):
    warehouse = db.get_or_404(Warehouse, warehouse_id)
    product_id = to_int(request.form.get("product_id"), 0)
    counted = to_int(request.form.get("counted_quantity"), -1)
    product = db.get_or_404(Product, product_id) if product_id else None

    if product is None or counted < 0:
        flash(_("Pick a product and enter a counted quantity of zero or more."), "danger")
    else:
        from ..services import StockError, adjust_stock

        try:
            item, delta = adjust_stock(product, warehouse, counted,
                                    note=clean(request.form, "note"))
            log_action("warehouse.adjust", "warehouse", warehouse.id,
                       f"{product.sku}: {delta:+d}")
            db.session.commit()
            flash(_("{name} adjusted by {delta:+d}.").format(name=product.name, delta=delta), "success")
        except StockError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return redirect(request.referrer or url_for("warehouses.index"))