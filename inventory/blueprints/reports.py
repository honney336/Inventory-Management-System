"""Reports: valuation, low stock, ABC, ageing, dead stock, supplier spend."""

from datetime import date

from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from sqlalchemy import func

from ..extensions import db
from ..i18n import translate as _
from ..models import (
    MOVE_ADJUSTMENT,
    Product,
    StockItem,
    StockMovement,
    Supplier,
    Warehouse,
)
from ..services import (
    abc_analysis,
    batch_summary,
    build_reorder_orders,
    dead_stock,
    low_stock_products,
    reorder_recommendation,
    stock_aging,
    supplier_performance,
    total_retail_value,
    total_stock_value,
)
from ..utils import csv_response, to_int

bp = Blueprint("reports", __name__, url_prefix="/reports")

REPORT_TITLES = {
    "valuation": "Stock valuation",
    "low-stock": "Low stock",
    "abc": "ABC analysis",
    "ageing": "Stock ageing",
    "dead-stock": "Dead stock",
    "expiry": "Expiry watch",
    "reorder": "Reorder suggestions",
    "suppliers": "Supplier spend",
    "audit": "Activity log",
}

REPORT_TEMPLATES = {
    "valuation": "reports/valuation.html",
    "low-stock": "reports/low_stock.html",
    "abc": "reports/abc.html",
    "ageing": "reports/ageing.html",
    "dead-stock": "reports/dead_stock.html",
    "expiry": "reports/expiry.html",
    "reorder": "reports/reorder.html",
    "suppliers": "reports/suppliers.html",
    "audit": "reports/audit.html",
}


def report_catalogue():
    """Titles translated for the visitor, templates resolved lazily."""
    return {
        key: {"title": _(REPORT_TITLES[key]), "template": template}
        for key, template in REPORT_TEMPLATES.items()
    }


@bp.get("")
@login_required
def index():
    stats = {
        "valuation": total_stock_value(),
        "low-stock": len(low_stock_products()),
        "abc": len(abc_analysis(limit=500)),
        "ageing": len(stock_aging()),
        "dead-stock": len(dead_stock()),
        "expiry": len(batch_summary()["batches"]),
        "reorder": len(low_stock_products()),
        "suppliers": db.session.execute(db.select(func.count(Supplier.id))).scalar_one(),
        "audit": db.session.execute(
            db.select(func.count(func.distinct(StockMovement.id)))
        ).scalar_one(),
    }
    return render_template(
        "reports/index.html",
        title="Reports",
        reports=report_catalogue(),
        stats=stats,
    )


@bp.get("/<report_key>")
@login_required
def show(report_key):
    if report_key not in REPORT_TEMPLATES:
        return render_template("errors/error.html", code=404,
                               message=_("Unknown report.")), 404

    data = {}
    if report_key == "valuation":
        warehouse_id = request.args.get("warehouse", type=int)
        query = db.select(Product).outerjoin(StockItem, StockItem.product_id == Product.id)
        if warehouse_id:
            query = query.where(StockItem.warehouse_id == warehouse_id)
        data["products"] = db.session.execute(
            query.where(Product.is_active_flag.is_(True)).order_by(Product.name)
        ).scalars().all()
        data["total"] = total_stock_value()
        data["retail"] = total_retail_value()
        data["potential_margin"] = data["retail"] - data["total"]
        data["warehouses"] = db.session.execute(
            db.select(Warehouse).order_by(Warehouse.code)
        ).scalars().all()
        data["warehouse_id"] = warehouse_id

    elif report_key == "low-stock":
        warehouse_id = request.args.get("warehouse", type=int)
        data["rows"] = low_stock_products(warehouse_id=warehouse_id)
        data["warehouses"] = db.session.execute(
            db.select(Warehouse).order_by(Warehouse.code)
        ).scalars().all()
        data["warehouse_id"] = warehouse_id
        data["reorder_plan"] = build_reorder_orders(warehouse_id)
        data["estimated_cost"] = round(
            sum(
                (p.suggested_order_qty or 0) * float(p.cost or 0)
                for p, _ in low_stock_products(warehouse_id)
            ),
            2,
        )

    elif report_key == "abc":
        days = to_int(request.args.get("days"), 365, minimum=1)
        data["rows"] = abc_analysis(limit=250)
        data["days"] = days

    elif report_key == "ageing":
        data["rows"] = stock_aging()

    elif report_key == "dead-stock":
        days = to_int(request.args.get("days"), 180, minimum=1)
        data["rows"] = dead_stock(days=days)
        data["days"] = days
        data["value"] = round(sum(p.stock_value for p in dead_stock(days=days)), 2)

    elif report_key == "expiry":
        summary = batch_summary(request.args.get("days", type=int))
        data.update(summary)
        data["days"] = request.args.get("days", type=int)

    elif report_key == "reorder":
        warehouse_id = request.args.get("warehouse", type=int)
        rows = low_stock_products(warehouse_id=warehouse_id)
        data["rows"] = [
            {
                "product": product,
                "on_hand": on_hand,
                "suggested": product.suggested_order_qty,
                "cost": round((product.suggested_order_qty or 0) * float(product.cost or 0), 2),
                "recommendation": reorder_recommendation(product),
            }
            for product, on_hand in rows
        ]
        data["total"] = round(sum(row["cost"] for row in data["rows"]), 2)
        data["warehouses"] = db.session.execute(
            db.select(Warehouse).order_by(Warehouse.code)
        ).scalars().all()
        data["warehouse_id"] = warehouse_id

    elif report_key == "suppliers":
        data["rows"] = supplier_performance()

    elif report_key == "audit":
        limit = to_int(request.args.get("limit"), 250, minimum=10)
        data["rows"] = db.session.execute(
            db.select(StockMovement).order_by(StockMovement.created_at.desc()).limit(limit)
        ).scalars().all()
        data["adjustments"] = db.session.execute(
            db.select(StockMovement).where(StockMovement.move_type == MOVE_ADJUSTMENT)
            .order_by(StockMovement.created_at.desc()).limit(limit)
        ).scalars().all()
        data["limit"] = limit

    return render_template(
        REPORT_TEMPLATES[report_key],
        title=_(REPORT_TITLES[report_key]),
        report_key=report_key,
        reports=report_catalogue(),
        **data,
    )


@bp.get("/<report_key>.csv")
@login_required
def export(report_key):
    if report_key not in REPORT_TEMPLATES:
        return Response(_("Unknown report"), status=404, mimetype="text/plain")

    stamp = date.today().strftime("%Y%m%d")
    headers, data = [], []

    if report_key == "valuation":
        headers = ["sku", "name", "category", "warehouse units", "on hand",
                   "cost", "value", "price", "retail value"]
        for product in db.session.execute(
            db.select(Product).where(Product.is_active_flag.is_(True)).order_by(Product.name)
        ).scalars():
            data.append([
                product.sku, product.name, product.category.name if product.category else "",
                product.quantity, product.quantity, float(product.cost or 0),
                round(product.stock_value, 2), float(product.price or 0),
                round(product.retail_value, 2),
            ])

    elif report_key == "low-stock":
        headers = ["sku", "name", "on hand", "reorder point", "suggested qty",
                   "unit cost", "estimated cost", "supplier"]
        for product, on_hand in low_stock_products():
            data.append([
                product.sku, product.name, on_hand, product.reorder_point,
                product.suggested_order_qty, float(product.cost or 0),
                round((product.suggested_order_qty or 0) * float(product.cost or 0), 2),
                product.supplier.name if product.supplier else "",
            ])

    elif report_key == "abc":
        headers = ["grade", "sku", "name", "units consumed", "consumption value",
                   "share %", "cumulative %"]
        for row in abc_analysis(limit=250):
            data.append([row["grade"], row["sku"], row["name"], row["consumed"],
                         round(row["value"], 2), row["share"], row["cumulative"]])

    elif report_key == "ageing":
        headers = ["age bucket", "units", "value at cost"]
        for label, units, value in stock_aging():
            data.append([label, units, round(value, 2)])

    elif report_key == "dead-stock":
        headers = ["sku", "name", "on hand", "value at cost", "last updated"]
        for product in dead_stock():
            data.append([product.sku, product.name, product.quantity,
                         round(product.stock_value, 2), product.updated_at.strftime("%Y-%m-%d")])

    elif report_key == "expiry":
        headers = ["state", "sku", "batch", "warehouse", "quantity", "expiry",
                   "days left", "value at cost"]
        for batch in batch_summary()["batches"]:
            data.append([
                batch.expiry_state, batch.product.sku if batch.product else "",
                batch.batch_no, batch.warehouse.code if batch.warehouse else "",
                batch.quantity,
                batch.expiry_date.isoformat() if batch.expiry_date else "",
                batch.days_to_expiry, round(batch.value, 2),
            ])

    elif report_key == "reorder":
        headers = ["sku", "product", "on hand", "current par", "suggested par",
                   "suggested order", "unit cost", "line cost", "supplier"]
        for product, on_hand in low_stock_products():
            recommendation = reorder_recommendation(product)
            suggested = (recommendation["reorder_quantity"] if recommendation
                         else product.suggested_order_qty)
            par = (recommendation["reorder_point"] if recommendation
                   else product.reorder_point)
            data.append([
                product.sku, product.name, on_hand, product.reorder_point, par,
                suggested, float(product.cost or 0),
                round((suggested or 0) * float(product.cost or 0), 2),
                product.supplier.name if product.supplier else "",
            ])

    elif report_key == "suppliers":
        headers = ["supplier", "orders", "spend"]
        for name, orders, spend in supplier_performance():
            data.append([name, orders, round(spend, 2)])

    elif report_key == "audit":
        headers = ["when", "sku", "product", "warehouse", "type", "quantity",
                   "balance after", "reference", "user"]
        for movement in db.session.execute(
            db.select(StockMovement).order_by(StockMovement.created_at.desc()).limit(2000)
        ).scalars():
            data.append([
                movement.created_at.strftime("%Y-%m-%d %H:%M"),
                movement.product.sku if movement.product else "",
                movement.product.name if movement.product else "",
                movement.warehouse.code if movement.warehouse else "",
                movement.move_type, movement.quantity, movement.balance_after,
                movement.reference or "",
                movement.user.username if movement.user else "",
            ])

    payload, filename = csv_response(f"{report_key}-{stamp}.csv", headers, data)
    return Response(
        payload,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )