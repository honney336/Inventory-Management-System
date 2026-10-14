"""JSON API for external tools, barcode scanners and integrations.

Two ways to authenticate: an existing browser session, or an `X-API-Key`
header matching the key stored in settings.
"""

from functools import wraps

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Category, Product, StockItem, StockMovement, Supplier, Warehouse
from ..services import (
    StockError,
    get_setting,
    issue_stock,
    log_action,
    receive_stock,
)
from ..utils import barcode_matches, clean, to_decimal, to_int

bp = Blueprint("api", __name__, url_prefix="/api")


def api_key_required(fn):
    """Allow session users or a valid X-API-Key header."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-API-Key", "")
        expected = get_setting("api_key", "")
        if expected and supplied and supplied == expected:
            request.api_key_auth = True
            return fn(*args, **kwargs)
        if current_user.is_authenticated:
            request.api_key_auth = False
            return fn(*args, **kwargs)
        return jsonify(ok=False, error="Authentication required"), 401

    return wrapper


def _product_json(product, warehouse_id=None):
    on_hand = product.stock_at(warehouse_id) if warehouse_id else product.quantity
    return {
        "id": product.id,
        "sku": product.sku,
        "barcode": product.barcode,
        "name": product.name,
        "description": product.description,
        "category": product.category.name if product.category else None,
        "supplier": product.supplier.name if product.supplier else None,
        "unit": product.unit,
        "cost": float(product.cost or 0),
        "price": float(product.price or 0),
        "tax_rate": float(product.tax_rate or 0),
        "on_hand": on_hand,
        "reorder_point": product.reorder_point,
        "reorder_quantity": product.reorder_quantity,
        "stock_status": product.stock_status,
        "location_bin": product.location_bin,
        "active": product.is_active,
        "stock_value": round(float(product.cost or 0) * on_hand, 2),
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


@bp.get("/health")
def health():
    return jsonify(ok=True, service="inventory-api", version="1.0")


@bp.get("/products")
@api_key_required
def list_products():
    term = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))
    warehouse_id = request.args.get("warehouse", type=int)

    query = db.select(Product)
    if term:
        query = query.where(
            or_(Product.name.ilike(f"%{term}%"), Product.sku.ilike(f"%{term}%"))
        )
    if request.args.get("low_stock") in {"1", "true"}:
        query = query.where(Product.reorder_point > 0)

    total = db.session.execute(
        db.select(func.count()).select_from(query.subquery())
    ).scalar_one()

    products = db.session.execute(
        query.order_by(Product.name).limit(per_page).offset((page - 1) * per_page)
    ).scalars().all()

    return jsonify(
        ok=True,
        page=page,
        per_page=per_page,
        total=int(total or 0),
        products=[_product_json(p, warehouse_id) for p in products],
    )


@bp.get("/products/<int:product_id>")
@api_key_required
def get_product(product_id):
    product = db.get_or_404(Product, product_id)
    warehouse_id = request.args.get("warehouse", type=int)
    return jsonify(ok=True, product=_product_json(product, warehouse_id))


@bp.get("/lookup")
@api_key_required
def lookup():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify(ok=False, error="code parameter required"), 400
    product = db.session.execute(
        db.select(Product).where(
            or_(barcode_matches(Product.barcode, code),
                func.lower(Product.sku) == code.lower())
        )
    ).scalars().first()
    if product is None:
        return jsonify(ok=False, error=f"no product matches '{code}'"), 404
    return jsonify(ok=True, product=_product_json(product))


@bp.post("/products")
@api_key_required
def create_product():
    payload = request.get_json(silent=True) or request.form.to_dict()
    sku = clean(payload, "sku", 60).upper()
    name = clean(payload, "name", 200)
    if not sku or not name:
        return jsonify(ok=False, error="sku and name are required"), 400

    product = Product(
        sku=sku,
        name=name,
        barcode=clean(payload, "barcode", 60) or None,
        description=clean(payload, "description"),
        unit=clean(payload, "unit", 24) or "each",
        cost=to_decimal(payload.get("cost")),
        price=to_decimal(payload.get("price")),
        reorder_point=to_int(payload.get("reorder_point"), 0, minimum=0),
        reorder_quantity=to_int(payload.get("reorder_quantity"), 1, minimum=1),
    )
    db.session.add(product)
    try:
        db.session.flush()
        log_action("api.product.create", "product", product.id, product.sku)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error="sku or barcode already exists"), 409

    return jsonify(ok=True, product=_product_json(product)), 201


@bp.patch("/products/<int:product_id>")
@api_key_required
def update_product(product_id):
    product = db.get_or_404(Product, product_id)
    payload = request.get_json(silent=True) or request.form.to_dict()

    for field in ("name", "description", "unit", "location_bin"):
        if field in payload:
            setattr(product, field, clean(payload, field))
    for field in ("cost", "price", "tax_rate"):
        if field in payload:
            setattr(product, field, to_decimal(payload.get(field)))
    for field in ("reorder_point", "reorder_quantity"):
        if field in payload:
            setattr(product, field, to_int(payload.get(field), 0, minimum=0))
    for field, foreign in (("category_id", Category), ("supplier_id", Supplier)):
        if field in payload:
            row = db.session.execute(
                db.select(foreign).where(foreign.id == to_int(payload.get(field), 0))
            ).scalar_one_or_none()
            setattr(product, field, row.id if row else None)

    try:
        log_action("api.product.update", "product", product.id, product.sku)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error="sku or barcode already exists"), 409

    return jsonify(ok=True, product=_product_json(product))


@bp.post("/stock/receive")
@api_key_required
def stock_receive():
    payload = request.get_json(silent=True) or request.form.to_dict()
    product = _lookup_product(payload)
    warehouse = _lookup_warehouse(payload)
    if product is None or warehouse is None:
        return jsonify(ok=False, error="valid product_id and warehouse_id required"), 400

    try:
        item = receive_stock(
            product,
            warehouse,
            to_int(payload.get("quantity"), 0),
            unit_cost=payload.get("unit_cost"),
            reference=clean(payload, "reference", 60),
            note=clean(payload, "note"),
        )
        db.session.commit()
    except StockError as exc:
        db.session.rollback()
        return jsonify(ok=False, error=str(exc)), 400

    return jsonify(
        ok=True,
        product=_product_json(product),
        warehouse=warehouse.code,
        on_hand=item.quantity,
    )


@bp.post("/stock/issue")
@api_key_required
def stock_issue():
    payload = request.get_json(silent=True) or request.form.to_dict()
    product = _lookup_product(payload)
    warehouse = _lookup_warehouse(payload)
    if product is None or warehouse is None:
        return jsonify(ok=False, error="valid product_id and warehouse_id required"), 400

    try:
        item = issue_stock(
            product,
            warehouse,
            to_int(payload.get("quantity"), 0),
            reference=clean(payload, "reference", 60),
            note=clean(payload, "note"),
        )
        db.session.commit()
    except StockError as exc:
        db.session.rollback()
        return jsonify(ok=False, error=str(exc)), 400

    return jsonify(
        ok=True,
        product=_product_json(product),
        warehouse=warehouse.code,
        on_hand=item.quantity,
    )


@bp.get("/movements")
@api_key_required
def movements():
    limit = min(500, max(1, request.args.get("limit", 100, type=int)))
    product_id = request.args.get("product_id", type=int)
    query = db.select(StockMovement).order_by(
        StockMovement.created_at.desc(), StockMovement.id.desc()
    )
    if product_id:
        query = query.where(StockMovement.product_id == product_id)

    rows = db.session.execute(query.limit(limit)).scalars().all()
    return jsonify(
        ok=True,
        movements=[
            {
                "id": m.id,
                "when": m.created_at.isoformat(),
                "sku": m.product.sku if m.product else None,
                "product_id": m.product_id,
                "warehouse": m.warehouse.code if m.warehouse else None,
                "type": m.move_type,
                "quantity": m.quantity,
                "balance_after": m.balance_after,
                "reference": m.reference,
                "note": m.note,
                "user": m.user.username if m.user else None,
            }
            for m in rows
        ],
    )


@bp.get("/low-stock")
@api_key_required
def low_stock():
    warehouse_id = request.args.get("warehouse", type=int)
    if warehouse_id:
        subq = db.select(StockItem.product_id).where(StockItem.warehouse_id == warehouse_id)
        candidates = db.session.execute(
            db.select(Product).where(
                Product.is_active_flag.is_(True),
                Product.reorder_point > 0,
                Product.id.in_(subq),
            )
        ).scalars().all()
        rows = [
            (p, p.stock_at(warehouse_id))
            for p in candidates
            if p.stock_at(warehouse_id) <= p.reorder_point
        ]
    else:
        rows = [(p, p.quantity) for p in db.session.execute(
            db.select(Product).where(
                Product.is_active_flag.is_(True), Product.reorder_point > 0
            )
        ).scalars() if p.quantity <= p.reorder_point]

    rows.sort(key=lambda pair: pair[1] - pair[0].reorder_point)
    return jsonify(
        ok=True,
        low_stock=[
            {
                "sku": p.sku,
                "name": p.name,
                "on_hand": on_hand,
                "reorder_point": p.reorder_point,
                "suggested_order_qty": p.suggested_order_qty,
                "supplier": p.supplier.name if p.supplier else None,
            }
            for p, on_hand in rows
        ],
    )


@bp.get("/summary")
@api_key_required
def summary():
    from ..services import total_stock_value

    counts = db.session.execute(
        db.select(
            func.count(func.distinct(Product.id)),
            func.coalesce(func.sum(StockItem.quantity), 0),
        )
        .select_from(StockItem)
        .join(Product, StockItem.product_id == Product.id)
    ).one()

    return jsonify(
        ok=True,
        products=int(counts[0] or 0),
        units=int(counts[1] or 0),
        stock_value=round(total_stock_value(), 2),
        warehouses=db.session.execute(
            db.select(func.count(Warehouse.id))
        ).scalar_one(),
    )


def _lookup_product(payload):
    product_id = to_int(payload.get("product_id"), 0)
    if product_id:
        return db.session.execute(
            db.select(Product).where(Product.id == product_id)
        ).scalar_one_or_none()
    code = (payload.get("sku") or payload.get("code") or "").strip()
    if not code:
        return None
    return db.session.execute(
        db.select(Product).where(
            or_(Product.sku == code, barcode_matches(Product.barcode, code))
        )
    ).scalars().first()


def _lookup_warehouse(payload):
    warehouse_id = to_int(payload.get("warehouse_id"), 0)
    if warehouse_id:
        return db.session.execute(
            db.select(Warehouse).where(Warehouse.id == warehouse_id)
        ).scalar_one_or_none()
    code = (payload.get("warehouse") or "").strip()
    if not code:
        return db.session.execute(
            db.select(Warehouse).where(Warehouse.is_active_flag.is_(True))
            .order_by(Warehouse.code).limit(1)
        ).scalar_one_or_none()
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.code == code.upper())
    ).scalar_one_or_none()