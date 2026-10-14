"""Purchase orders: draft, receive against, cancel."""

from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..decorators import manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import (
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_DRAFT,
    ORDER_PARTIAL,
    Product,
    PurchaseOrder,
    Supplier,
    Warehouse,
)
from ..notifier import check_expiry, check_stock_levels
from ..services import (
    StockError,
    build_reorder_orders,
    cancel_purchase_order,
    create_purchase_order,
    get_setting,
    log_action,
    receive_purchase_order,
)
from ..utils import clean, csv_response, paginate, to_date, to_decimal, to_int

bp = Blueprint("purchasing", __name__, url_prefix="/purchase-orders")


@bp.get("")
@login_required
def index():
    term = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")

    query = db.select(PurchaseOrder).join(Supplier, PurchaseOrder.supplier_id == Supplier.id)
    if term:
        query = query.where(
            or_(PurchaseOrder.number.ilike(f"%{term}%"), Supplier.name.ilike(f"%{term}%"))
        )
    if status in {ORDER_DRAFT, ORDER_PARTIAL, ORDER_COMPLETE, ORDER_CANCELLED}:
        query = query.where(PurchaseOrder.status == status)

    result = paginate(
        query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()),
        endpoint="purchasing.index",
    )

    return render_template(
        "purchasing/index.html",
        title="Purchase orders",
        orders=result["items"],
        pagination=result["pagination"],
        term=term,
        status=status,
    )


@bp.route("/new", methods=["GET", "POST"])
@manager_required
def create():
    if request.method == "POST":
        supplier = _pick_supplier(request.form)
        warehouse = _pick_warehouse(request.form)
        lines = _parse_lines(request.form, cost_field="unit_cost")

        if supplier is None or warehouse is None:
            flash(_("Choose a supplier and a destination warehouse."), "danger")
        elif not lines:
            flash(_("Add at least one line with a quantity above zero."), "danger")
        else:
            try:
                order = create_purchase_order(
                    supplier,
                    warehouse,
                    lines,
                    expected_date=to_date(request.form.get("expected_date")),
                    note=clean(request.form, "note"),
                    reference=clean(request.form, "reference", 80),
                )
                db.session.commit()
                flash(_("Purchase order {number} created.").format(number=order.number), "success")
                return redirect(url_for("purchasing.detail", order_id=order.id))
            except StockError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
            except IntegrityError:
                db.session.rollback()
                flash(_("Could not create the order, the document number collided. Try again."))

    return render_template(
        "purchasing/form.html",
        title="New purchase order",
        suppliers=_suppliers(),
        warehouses=_warehouses(),
        products=_products(),
        default_lead=get_setting("default_lead_time_days", "7"),
    )


@bp.route("/from-low-stock", methods=["POST"])
@manager_required
def from_low_stock():
    """Draft one purchase order per supplier for everything below its reorder point."""
    warehouse_id = to_int(request.form.get("warehouse_id"), 0) or None
    grouped = build_reorder_orders(warehouse_id)

    if not grouped:
        flash(_("Nothing is below its reorder point right now."), "info")
        return redirect(url_for("purchasing.index"))

    warehouse = _pick_warehouse({"warehouse_id": warehouse_id}) if warehouse_id else _default_warehouse()
    if warehouse is None:
        flash(_("Create a warehouse first."), "danger")
        return redirect(url_for("warehouses.index"))

    made = 0
    for supplier, products in grouped:
        lines = [
            (p, p.suggested_order_qty or p.reorder_quantity or 1, p.cost)
            for p in products
            if p.suggested_order_qty > 0
        ]
        if not lines:
            continue
        try:
            create_purchase_order(
                supplier,
                warehouse,
                lines,
                expected_date=date.today() + timedelta(days=supplier.lead_time_days or 7),
                note="Auto-generated from reorder points",
            )
            made += 1
        except StockError as exc:
            flash(_("Skipped {supplier}: {error}").format(supplier=supplier.name, error=exc), "warning")

    if made:
        db.session.commit()
        flash(_("Drafted {count} purchase order(s) from reorder points.").format(count=made), "success")
    else:
        db.session.rollback()

    return redirect(url_for("purchasing.index"))


@bp.get("/<int:order_id>")
@login_required
def detail(order_id):
    order = db.get_or_404(PurchaseOrder, order_id)
    return render_template(
        "purchasing/detail.html",
        title=order.number,
        order=order,
    )


@bp.route("/<int:order_id>/receive", methods=["GET", "POST"])
@manager_required
def receive(order_id):
    order = db.get_or_404(PurchaseOrder, order_id)
    if not order.can_receive:
        flash(_("That order can no longer be received."), "warning")
        return redirect(url_for("purchasing.detail", order_id=order.id))

    if request.method == "POST":
        quantities = _parse_quantities(request.form)
        batch_fields = {
            line_id: {
                "batch_no": clean(request.form, f"batch_{line_id}", 60),
                "expiry_date": to_date(request.form.get(f"expiry_{line_id}")),
            }
            for line_id in quantities
        }
        try:
            receive_purchase_order(order, quantities, clean(request.form, "note"),
                                   batch_fields=batch_fields)
            db.session.commit()
            check_stock_levels([line.product for line in order.lines])
            check_expiry()
            flash(_("{number} received into {warehouse}.").format(number=order.number, warehouse=order.warehouse.name), "success")
            return redirect(url_for("purchasing.detail", order_id=order.id))
        except StockError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template("purchasing/receive.html", title=f"Receive {order.number}",
                           order=order)


@bp.post("/<int:order_id>/cancel")
@manager_required
def cancel(order_id):
    order = db.get_or_404(PurchaseOrder, order_id)
    cancel_purchase_order(order, clean(request.form, "reason"))
    db.session.commit()
    flash(_("{number} cancelled.").format(number=order.number), "info")
    return redirect(url_for("purchasing.detail", order_id=order.id))


@bp.post("/<int:order_id>/delete")
@manager_required
def delete(order_id):
    order = db.get_or_404(PurchaseOrder, order_id)
    if order.status != ORDER_DRAFT:
        flash(_("Only draft orders can be deleted. Cancel it instead."), "warning")
        return redirect(url_for("purchasing.detail", order_id=order.id))

    log_action("purchase_order.delete", "purchase_order", order.id, order.number)
    db.session.delete(order)
    db.session.commit()
    flash(_("{number} deleted.").format(number=order.number), "success")
    return redirect(url_for("purchasing.index"))


@bp.get("/export.csv")
@login_required
def export_csv():
    orders = db.session.execute(
        db.select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).limit(5000)
    ).scalars().all()
    data = [
        [
            o.number, o.supplier.name if o.supplier else "", o.status,
            o.expected_date.isoformat() if o.expected_date else "",
            o.warehouse.code if o.warehouse else "", o.total_units,
            f"{o.total_cost:.2f}", o.created_at.strftime("%Y-%m-%d"),
        ]
        for o in orders
    ]
    payload, filename = csv_response(
        f"purchase-orders-{date.today():%Y%m%d}.csv",
        ["number", "supplier", "status", "expected", "warehouse", "units", "total", "created"],
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


def _parse_lines(form, cost_field="unit_cost"):
    lines = []
    ids = form.getlist("product_id")
    for index, raw_id in enumerate(ids):
        product_id = to_int(raw_id, 0)
        quantity = to_int(form.getlist("quantity")[index], 0) if index < len(form.getlist("quantity")) else 0
        if not product_id or quantity <= 0:
            continue
        product = db.session.execute(
            db.select(Product).where(Product.id == product_id)
        ).scalar_one_or_none()
        if product is None:
            continue
        costs = form.getlist(cost_field)
        cost = to_decimal(costs[index]) if index < len(costs) else to_decimal(product.cost)
        lines.append((product, quantity, cost))
    return lines


def _pick_supplier(form):
    supplier_id = to_int(form.get("supplier_id"), 0)
    if not supplier_id:
        return None
    return db.session.execute(
        db.select(Supplier).where(Supplier.id == supplier_id)
    ).scalar_one_or_none()


def _pick_warehouse(form):
    warehouse_id = to_int(form.get("warehouse_id"), 0)
    if not warehouse_id:
        return None
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()


def _default_warehouse():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True))
        .order_by(Warehouse.code).limit(1)
    ).scalar_one_or_none()


def _suppliers():
    return db.session.execute(
        db.select(Supplier).where(Supplier.is_active_flag.is_(True)).order_by(Supplier.name)
    ).scalars().all()


def _warehouses():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
    ).scalars().all()


def _products():
    return db.session.execute(
        db.select(Product).where(Product.is_active_flag.is_(True)).order_by(Product.name)
    ).scalars().all()