"""Barcode scanning desk: camera lookup plus quick receive and issue."""

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required
from sqlalchemy import or_

from ..decorators import manager_required
from ..extensions import db
from ..models import Product, Warehouse
from ..services import StockError, issue_stock, receive_stock
from ..utils import barcode_matches, clean, to_int

bp = Blueprint("scan", __name__, url_prefix="/scan")


@bp.get("")
@login_required
def index():
    return render_template(
        "scan/index.html",
        title="Scan",
        warehouses=db.session.execute(
            db.select(Warehouse).where(Warehouse.is_active_flag.is_(True))
            .order_by(Warehouse.code)
        ).scalars().all(),
    )


@bp.get("/lookup")
@login_required
def lookup():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify(ok=False, error="no code supplied"), 400

    product = db.session.execute(
        db.select(Product).where(
            or_(barcode_matches(Product.barcode, code),
                db.func.lower(Product.sku) == code.lower())
        )
    ).scalars().first()

    if product is None:
        return jsonify(ok=False, error=f"no product matches '{code}'"), 404

    stock = {
        w.code: product.stock_at(w.id)
        for w in db.session.execute(
            db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
        ).scalars()
    }

    return jsonify(
        ok=True,
        id=product.id,
        sku=product.sku,
        name=product.name,
        unit=product.unit,
        price=float(product.price or 0),
        on_hand=product.quantity,
        reorder_point=product.reorder_point,
        status=product.stock_status,
        detail_url=product_detail_url(product.id),
        by_warehouse=stock,
    )


@bp.post("/move")
@manager_required
def move():
    """Quick in/out from the scan screen."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    product_id = to_int(payload.get("product_id"), 0)
    warehouse_id = to_int(payload.get("warehouse_id"), 0)
    quantity = to_int(payload.get("quantity"), 0)
    direction = (payload.get("direction") or "in").strip()

    product = db.session.execute(
        db.select(Product).where(Product.id == product_id)
    ).scalar_one_or_none()
    warehouse = db.session.execute(
        db.select(Warehouse).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()

    if product is None or warehouse is None:
        return jsonify(ok=False, error="unknown product or warehouse"), 400

    note = clean(payload, "note")
    try:
        if direction == "out":
            item = issue_stock(product, warehouse, quantity, reference="SCAN", note=note)
        else:
            item = receive_stock(product, warehouse, quantity,
                                 unit_cost=payload.get("unit_cost"),
                                 reference="SCAN", note=note or "Scan station")
        db.session.commit()
    except StockError as exc:
        db.session.rollback()
        return jsonify(ok=False, error=str(exc)), 400

    return jsonify(
        ok=True,
        direction=direction,
        on_hand=item.quantity,
        message=f"{'Received' if direction != 'out' else 'Issued'} {quantity} "
                f"{product.unit} of {product.name}",
    )


def product_detail_url(product_id):
    from flask import url_for

    return url_for("products.detail", product_id=product_id)