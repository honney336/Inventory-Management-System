"""Sales orders: draft, fulfil from stock, cancel."""

from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_

from ..decorators import manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import (
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_DRAFT,
    ORDER_PARTIAL,
    Product,
    SalesOrder,
    Warehouse,
    utcnow,
)
from ..notifier import check_stock_levels
from ..services import (
    StockError,
    allocate_batches,
    cancel_sales_order,
    complete_sales_order,
    create_sales_order,
    log_action,
)
from ..utils import clean, csv_response, paginate, to_decimal, to_int

bp = Blueprint("sales", __name__, url_prefix="/sales-orders")


@bp.get("")
@login_required
def index():
    term = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")
    window = request.args.get("window", "30")

    query = db.select(SalesOrder)
    if term:
        query = query.where(
            or_(
                SalesOrder.number.ilike(f"%{term}%"),
                SalesOrder.customer_name.ilike(f"%{term}%"),
                SalesOrder.customer_email.ilike(f"%{term}%"),
            )
        )
    if status in {ORDER_DRAFT, ORDER_PARTIAL, ORDER_COMPLETE, ORDER_CANCELLED}:
        query = query.where(SalesOrder.status == status)

    result = paginate(
        query.order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc()),
        endpoint="sales.index",
    )

    since = utcnow() - timedelta(days={"7": 7, "30": 30, "365": 365}.get(window, 30))
    completed = db.session.execute(
        db.select(SalesOrder).where(
            SalesOrder.status == ORDER_COMPLETE, SalesOrder.completed_at >= since
        )
    ).scalars().all()

    period_total = round(sum(o.total for o in completed), 2)
    period_units = sum(o.total_units for o in completed)

    return render_template(
        "sales/index.html",
        title="Sales orders",
        orders=result["items"],
        pagination=result["pagination"],
        term=term,
        status=status,
        window=window,
        completed_count=len(completed),
        period_total=period_total,
        period_units=period_units,
    )


@bp.route("/new", methods=["GET", "POST"])
@manager_required
def create():
    if request.method == "POST":
        customer = clean(request.form, "customer_name", 160)
        warehouse = _pick_warehouse(request.form)
        lines = _parse_lines(request.form)

        if not customer:
            flash(_("Customer name is required."), "danger")
        elif warehouse is None:
            flash(_("Choose the warehouse to ship from."), "danger")
        elif not lines:
            flash(_("Add at least one line with a quantity above zero."), "danger")
        else:
            try:
                order = create_sales_order(
                    customer,
                    warehouse,
                    lines,
                    note=clean(request.form, "note"),
                    reference=clean(request.form, "reference", 80),
                    customer_email=clean(request.form, "customer_email", 180),
                    customer_phone=clean(request.form, "customer_phone", 40),
                )
                db.session.commit()
                flash(_("Sales order {number} created.").format(number=order.number), "success")
                return redirect(url_for("sales.detail", order_id=order.id))
            except StockError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

    return render_template(
        "sales/form.html",
        title="New sales order",
        warehouses=_warehouses(),
        products=_products(),
    )


@bp.get("/<int:order_id>")
@login_required
def detail(order_id):
    order = db.get_or_404(SalesOrder, order_id)
    return render_template("sales/detail.html", title=order.number, order=order)


@bp.route("/<int:order_id>/fulfil", methods=["GET", "POST"])
@manager_required
def fulfil(order_id):
    order = db.get_or_404(SalesOrder, order_id)
    if not order.can_complete:
        flash(_("That order can no longer be fulfilled."), "warning")
        return redirect(url_for("sales.detail", order_id=order.id))

    if request.method == "POST":
        quantities = _parse_quantities(request.form)
        try:
            complete_sales_order(order, quantities, clean(request.form, "note"))
            db.session.commit()
            check_stock_levels([line.product for line in order.lines])
            flash(_("{number} fulfilled from {warehouse}.").format(number=order.number, warehouse=order.warehouse.name), "success")
            return redirect(url_for("sales.detail", order_id=order.id))
        except StockError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template(
        "sales/fulfil.html",
        title=_("Fulfil {number}").format(number=order.number),
        order=order,
        allocation=_fefo_plan(order),
    )


def _fefo_plan(order):
    """Which batches each line will draw from, so the picker can show it."""
    plan = {}
    for line in order.lines:
        if line.outstanding <= 0 or not line.product.track_batches:
            continue
        picks, short = allocate_batches(line.product, order.warehouse, line.outstanding)
        plan[line.id] = {
            "picks": [
                {
                    "batch_no": batch.batch_no,
                    "quantity": take,
                    "expiry_date": batch.expiry_date,
                    "value": round(batch.value, 2),
                }
                for batch, take in picks
            ],
            "short": short,
        }
    return plan


@bp.post("/<int:order_id>/cancel")
@manager_required
def cancel(order_id):
    order = db.get_or_404(SalesOrder, order_id)
    cancel_sales_order(order, clean(request.form, "reason"))
    db.session.commit()
    flash(_("{number} cancelled.").format(number=order.number), "info")
    return redirect(url_for("sales.detail", order_id=order.id))


@bp.post("/<int:order_id>/delete")
@manager_required
def delete(order_id):
    order = db.get_or_404(SalesOrder, order_id)
    if order.status != ORDER_DRAFT:
        flash(_("Only draft orders can be deleted. Cancel it instead."), "warning")
        return redirect(url_for("sales.detail", order_id=order.id))

    log_action("sales_order.delete", "sales_order", order.id, order.number)
    db.session.delete(order)
    db.session.commit()
    flash(_("{number} deleted.").format(number=order.number), "success")
    return redirect(url_for("sales.index"))


@bp.get("/export.csv")
@login_required
def export_csv():
    orders = db.session.execute(
        db.select(SalesOrder).order_by(SalesOrder.created_at.desc()).limit(5000)
    ).scalars().all()
    data = [
        [
            o.number, o.customer_name, o.customer_email or "", o.status,
            o.warehouse.code if o.warehouse else "", o.total_units,
            f"{o.total:.2f}", o.created_at.strftime("%Y-%m-%d"),
            o.completed_at.strftime("%Y-%m-%d") if o.completed_at else "",
        ]
        for o in orders
    ]
    payload, filename = csv_response(
        f"sales-orders-{date.today():%Y%m%d}.csv",
        ["number", "customer", "email", "status", "warehouse", "units", "total",
         "created", "completed"],
        data,
    )
    return Response(payload, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _parse_quantities(form):
    """Turn qty_<line_id>=<n> fields into {line_id: n}."""
    quantities = {}
    for key, value in form.items():
        if key.startswith("qty_") and key[4:].isdigit():
            quantities[int(key[4:])] = to_int(value, 0)
    return quantities


def _parse_lines(form):
    lines = []
    ids = form.getlist("product_id")
    quantities = form.getlist("quantity")
    prices = form.getlist("unit_price")
    for index, raw_id in enumerate(ids):
        product_id = to_int(raw_id, 0)
        quantity = to_int(quantities[index], 0) if index < len(quantities) else 0
        if not product_id or quantity <= 0:
            continue
        product = db.session.execute(
            db.select(Product).where(Product.id == product_id)
        ).scalar_one_or_none()
        if product is None:
            continue
        price = to_decimal(prices[index]) if index < len(prices) else to_decimal(product.price)
        lines.append((product, quantity, price))
    return lines


def _pick_warehouse(form):
    warehouse_id = to_int(form.get("warehouse_id"), 0)
    if not warehouse_id:
        return None
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()


def _warehouses():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
    ).scalars().all()


def _products():
    return db.session.execute(
        db.select(Product).where(Product.is_active_flag.is_(True)).order_by(Product.name)
    ).scalars().all()