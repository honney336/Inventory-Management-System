"""Stock movements: the ledger view plus receipt, issue, adjustment and transfer."""

from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from ..decorators import manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import (
    MOVE_RECEIPT,
    MOVE_RETURN,
    MOVE_SALE,
    MOVE_TYPES,
    Product,
    StockMovement,
    Warehouse,
    utcnow,
)
from ..notifier import check_expiry, check_stock_levels
from ..services import (
    StockError,
    adjust_stock,
    issue_stock,
    log_action,
    receive_stock,
    record_customer_return,
    transfer_stock,
)
from ..utils import clean, csv_response, paginate, to_date, to_int

bp = Blueprint("stock", __name__, url_prefix="/stock")

WINDOW_DAYS = {
    "today": 0,
    "7": 7,
    "30": 30,
    "90": 90,
    "365": 365,
}


@bp.get("/movements")
@login_required
def movements():
    term = (request.args.get("q") or "").strip()
    move_type = request.args.get("type", "")
    warehouse_id = request.args.get("warehouse", type=int)
    product_id = request.args.get("product", type=int)
    window = request.args.get("window", "30")

    query = db.select(StockMovement).join(
        Product, StockMovement.product_id == Product.id
    )

    if term:
        query = query.where(
            db.or_(
                Product.name.ilike(f"%{term}%"),
                Product.sku.ilike(f"%{term}%"),
                StockMovement.reference.ilike(f"%{term}%"),
                StockMovement.note.ilike(f"%{term}%"),
            )
        )
    if move_type in MOVE_TYPES:
        query = query.where(StockMovement.move_type == move_type)
    if warehouse_id:
        query = query.where(StockMovement.warehouse_id == warehouse_id)
    if product_id:
        query = query.where(StockMovement.product_id == product_id)

    days = WINDOW_DAYS.get(window, 30)
    since = utcnow() - timedelta(days=days)
    if window != "all":
        query = query.where(StockMovement.created_at >= since)

    result = paginate(
        query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()),
        endpoint="stock.movements",
    )

    inbound = db.session.execute(
        db.select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.created_at >= since, StockMovement.quantity > 0
        )
    ).scalar_one()
    outbound = db.session.execute(
        db.select(func.coalesce(func.sum(StockMovement.quantity), 0)).where(
            StockMovement.created_at >= since, StockMovement.quantity < 0
        )
    ).scalar_one()

    return render_template(
        "stock/movements.html",
        title="Stock movements",
        movements=result["items"],
        pagination=result["pagination"],
        warehouses=_warehouses(),
        move_types=MOVE_TYPES,
        term=term,
        selected_type=move_type,
        warehouse_id=warehouse_id,
        window=window,
        inbound=int(inbound or 0),
        outbound=abs(int(outbound or 0)),
    )


@bp.get("/movements.csv")
@login_required
def movements_csv():
    rows = db.session.execute(
        db.select(StockMovement)
        .order_by(StockMovement.created_at.desc())
        .limit(5000)
    ).scalars().all()

    data = [
        [
            m.created_at.strftime("%Y-%m-%d %H:%M"),
            m.product.sku if m.product else "",
            m.product.name if m.product else "",
            m.warehouse.code if m.warehouse else "",
            m.move_type,
            m.quantity,
            m.balance_after,
            m.reference or "",
            m.note or "",
            m.user.username if m.user else "",
        ]
        for m in rows
    ]
    payload, filename = csv_response(
        f"movements-{date.today():%Y%m%d}.csv",
        ["when", "sku", "product", "warehouse", "type", "quantity",
         "balance_after", "reference", "note", "user"],
        data,
    )
    return Response(payload, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/receive", methods=["GET", "POST"])
@manager_required
def receive():
    if request.method == "POST":
        product = _pick_product(request.form)
        warehouse = _pick_warehouse(request.form)
        quantity = to_int(request.form.get("quantity"), 0)

        if product is None or warehouse is None:
            flash(_("Choose both a product and a warehouse."), "danger")
        else:
            try:
                receive_stock(
                    product,
                    warehouse,
                    quantity,
                    move_type=MOVE_RECEIPT,
                    unit_cost=request.form.get("unit_cost"),
                    reference=clean(request.form, "reference", 60),
                    note=clean(request.form, "note"),
                    batch_no=clean(request.form, "batch_no", 60) or None,
                    expiry_date=to_date(request.form.get("expiry_date")),
                )
                log_action("stock.receive", "product", product.id,
                           f"{product.sku} +{quantity} @ {warehouse.code}")
                db.session.commit()
                check_stock_levels([product])
                check_expiry()
                flash(_("Received {quantity} × {name}.").format(quantity=quantity, name=product.name), "success")
                return redirect(url_for("stock.receive"))
            except StockError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

    return render_template(
        "stock/receive.html",
        title="Receive stock",
        products=_products(),
        warehouses=_warehouses(),
    )


@bp.route("/issue", methods=["GET", "POST"])
@manager_required
def issue():
    if request.method == "POST":
        product = _pick_product(request.form)
        warehouse = _pick_warehouse(request.form)
        quantity = to_int(request.form.get("quantity"), 0)

        if product is None or warehouse is None:
            flash(_("Choose both a product and a warehouse."), "danger")
        else:
            try:
                issue_stock(
                    product,
                    warehouse,
                    quantity,
                    move_type=MOVE_RETURN if _is_return(request.form) else MOVE_SALE,
                    reference=clean(request.form, "reference", 60),
                    note=clean(request.form, "note"),
                )
                log_action("stock.issue", "product", product.id,
                           f"{product.sku} -{quantity} @ {warehouse.code}")
                db.session.commit()
                check_stock_levels([product])
                flash(_("Issued {quantity} × {name}.").format(quantity=quantity, name=product.name), "success")
                return redirect(url_for("stock.issue"))
            except StockError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

    return render_template(
        "stock/issue.html",
        title="Issue stock",
        products=_products(),
        warehouses=_warehouses(),
    )


@bp.post("/return")
@manager_required
def customer_return():
    product = _pick_product(request.form)
    warehouse = _pick_warehouse(request.form)
    if product is None or warehouse is None:
        flash(_("Choose both a product and a warehouse."), "danger")
    else:
        try:
            record_customer_return(
                product,
                warehouse,
                to_int(request.form.get("quantity"), 0),
                reference=clean(request.form, "reference", 60),
                note=clean(request.form, "note"),
                batch_no=clean(request.form, "batch_no", 60) or None,
                expiry_date=to_date(request.form.get("expiry_date")),
            )
            db.session.commit()
            check_stock_levels([product])
            flash(_("Return booked back into stock."), "success")
        except StockError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return redirect(url_for("stock.receive"))


@bp.get("/adjust")
@manager_required
def adjust_form():
    return render_template(
        "stock/adjust.html",
        title="Stock adjustment",
        products=_products(),
        warehouses=_warehouses(),
    )


@bp.post("/adjust")
@manager_required
def adjust():
    product = _pick_product(request.form)
    warehouse = _pick_warehouse(request.form)
    counted = to_int(request.form.get("counted_quantity"), -1)

    if product is None or warehouse is None:
        flash(_("Choose both a product and a warehouse."), "danger")
    elif counted < 0:
        flash(_("Enter the counted quantity."), "danger")
    else:
        try:
            item, delta = adjust_stock(product, warehouse, counted,
                                    note=clean(request.form, "note"))
            log_action("stock.adjust", "product", product.id,
                       f"{product.sku} @ {warehouse.code}: {delta:+d}")
            db.session.commit()
            flash(
                _("{name} in {warehouse} corrected by {delta:+d} (now {counted}).").format(
                    name=product.name, warehouse=warehouse.name,
                    delta=delta, counted=counted,
                ),
                "success",
            )
        except StockError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return redirect(url_for("stock.movements"))


@bp.route("/transfer", methods=["GET", "POST"])
@manager_required
def transfer():
    if request.method == "POST":
        product = _pick_product(request.form)
        source = _pick_warehouse(request.form, field="source_id")
        destination = _pick_warehouse(request.form, field="destination_id")
        quantity = to_int(request.form.get("quantity"), 0)

        if product is None or source is None or destination is None:
            flash(_("Choose a product, a source and a destination."), "danger")
        else:
            try:
                transfer_stock(product, source, destination, quantity,
                               note=clean(request.form, "note"))
                log_action("stock.transfer", "product", product.id,
                           f"{product.sku} {quantity} {source.code}→{destination.code}")
                db.session.commit()
                check_expiry()
                flash(
                    _("Moved {quantity} × {name} to {destination}.").format(
                        quantity=quantity, name=product.name, destination=destination.name
                    ),
                    "success",
                )
                return redirect(url_for("stock.transfer"))
            except StockError as exc:
                db.session.rollback()
                flash(str(exc), "danger")

    return render_template(
        "stock/transfer.html",
        title="Transfer stock",
        products=_products(),
        warehouses=_warehouses(),
    )


def _pick_product(form):
    product_id = to_int(form.get("product_id"), 0)
    if not product_id:
        return None
    return db.session.execute(
        db.select(Product).where(Product.id == product_id)
    ).scalar_one_or_none()


def _pick_warehouse(form, field="warehouse_id"):
    warehouse_id = to_int(form.get(field), 0)
    if not warehouse_id:
        return None
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()


def _is_return(form):
    return (form.get("move_type") or MOVE_SALE).strip() == MOVE_RETURN


def _products():
    return db.session.execute(
        db.select(Product).where(Product.is_active_flag.is_(True)).order_by(Product.name)
    ).scalars().all()


def _warehouses():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
    ).scalars().all()