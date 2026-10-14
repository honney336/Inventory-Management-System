"""Landing page: the numbers a buyer or manager checks first thing."""

from datetime import timedelta

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user

from ..i18n import translate as _
from sqlalchemy import func

from ..extensions import db
from ..models import (
    MOVE_ADJUSTMENT,
    ORDER_COMPLETE,
    ORDER_DRAFT,
    ORDER_PARTIAL,
    Product,
    PurchaseOrder,
    SalesOrder,
    StockItem,
    StockMovement,
    Supplier,
    Warehouse,
    utcnow,
)
from ..services import (
    batch_summary,
    daily_movements,
    low_stock_products,
    stock_value_by_category,
    top_sellers,
    total_retail_value,
    total_stock_value,
)

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))

    warehouse_id = request.args.get("warehouse", type=int)
    warehouses = db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True)).order_by(Warehouse.code)
    ).scalars().all()

    active_products = db.session.execute(
        db.select(func.count(Product.id)).where(Product.is_active_flag.is_(True))
    ).scalar_one()

    total_units = db.session.execute(
        db.select(func.coalesce(func.sum(StockItem.quantity), 0))
    ).scalar_one()

    scoped_query = db.select(func.coalesce(func.sum(StockItem.quantity), 0))
    if warehouse_id:
        scoped_query = scoped_query.where(StockItem.warehouse_id == warehouse_id)
    scoped_units = db.session.execute(scoped_query).scalar_one()

    low = low_stock_products(warehouse_id=warehouse_id)
    out_of_stock = [row for row in low if row[1] <= 0]

    since_30 = utcnow() - timedelta(days=30)
    received_value = db.session.execute(
        db.select(func.coalesce(func.sum(StockMovement.quantity * Product.cost), 0))
        .select_from(StockMovement)
        .join(Product, StockMovement.product_id == Product.id)
        .where(StockMovement.quantity > 0, StockMovement.created_at >= since_30)
    ).scalar_one()

    open_purchase_orders = db.session.execute(
        db.select(func.count(PurchaseOrder.id)).where(
            PurchaseOrder.status.in_([ORDER_DRAFT, ORDER_PARTIAL])
        )
    ).scalar_one()

    open_sales_orders = db.session.execute(
        db.select(func.count(SalesOrder.id)).where(
            SalesOrder.status.in_([ORDER_DRAFT, ORDER_PARTIAL])
        )
    ).scalar_one()

    completed_sales = db.session.execute(
        db.select(SalesOrder).where(
            SalesOrder.status == ORDER_COMPLETE, SalesOrder.completed_at >= since_30
        )
    ).scalars().all()
    sales_30 = round(sum(o.total for o in completed_sales), 2)

    recent_movements = db.session.execute(
        db.select(StockMovement)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(12)
    ).scalars().all()

    adjustments = db.session.execute(
        db.select(func.count(StockMovement.id)).where(
            StockMovement.move_type == MOVE_ADJUSTMENT, StockMovement.created_at >= since_30
        )
    ).scalar_one()

    labels, inbound, outbound = daily_movements(14)
    categories = stock_value_by_category()[:8]
    sellers = top_sellers(limit=8, days=90)
    batches = batch_summary()

    return render_template(
        "dashboard.html",
        title=_("Dashboard"),
        warehouses=warehouses,
        warehouse_id=warehouse_id,
        active_products=int(active_products or 0),
        total_units=int(total_units or 0),
        scoped_units=int(scoped_units or 0),
        stock_value=total_stock_value(),
        retail_value=total_retail_value(),
        low_stock=low,
        out_of_stock=out_of_stock,
        received_value=float(received_value or 0),
        open_purchase_orders=int(open_purchase_orders or 0),
        open_sales_orders=int(open_sales_orders or 0),
        sales_30=sales_30,
        sales_30_count=len(completed_sales),
        adjustments=int(adjustments or 0),
        recent_movements=recent_movements,
        suppliers=db.session.execute(
            db.select(func.count(Supplier.id))
        ).scalar_one(),
        chart={"labels": labels, "inbound": inbound, "outbound": outbound},
        category_chart=categories,
        top_sellers=sellers,
        batches=batches,
    )