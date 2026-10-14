from datetime import date, timedelta
from math import ceil, sqrt
from statistics import pstdev

from flask import current_app, request
from flask_login import current_user
from sqlalchemy import func

from .extensions import db
from .models import (
    MOVE_ADJUSTMENT,
    MOVE_RECEIPT,
    MOVE_RETURN,
    MOVE_SALE,
    MOVE_TRANSFER_IN,
    MOVE_TRANSFER_OUT,
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_DRAFT,
    ORDER_PARTIAL,
    AuditLog,
    Batch,
    Category,
    Counter,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    SalesOrder,
    SalesOrderLine,
    Setting,
    StockItem,
    StockMovement,
    Supplier,
    Warehouse,
    utcnow,
)
from .utils import to_decimal, to_int
from .i18n import translate as _

DEFAULT_SETTINGS = {
    "company_name": "Inventory System",
    "currency_symbol": "$",
    "low_stock_threshold": "10",
    "default_lead_time_days": "7",
    "allow_negative_stock": "false",
    "default_locale": "en",
    "service_level": "95",
    "review_period_days": "30",
    "expiry_warning_days": "90",
    "notify_webhook_url": "",
}

TRUTHY = {"1", "true", "yes", "on"}


class StockError(Exception):
    pass


def get_setting(key, default=None):
    row = db.session.get(Setting, key)
    if row is None or row.value is None:
        return default if default is not None else DEFAULT_SETTINGS.get(key, "")
    return row.value


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    return row


def init_settings():
    for key, value in DEFAULT_SETTINGS.items():
        if db.session.get(Setting, key) is None:
            db.session.add(Setting(key=key, value=value))
    db.session.commit()


def negatives_allowed():
    return str(get_setting("allow_negative_stock", "false")).lower() in TRUTHY


def expiry_warning_days():
    return to_int(get_setting("expiry_warning_days", "90"), 90, minimum=1)


def client_ip():
    """The peer address, plus the forwarding header only behind a known proxy."""
    if not request:
        return None
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]


def log_action(action, entity=None, entity_id=None, details=None):
    entry = AuditLog(
        user_id=getattr(current_user, "id", None),
        action=action,
        entity=entity,
        entity_id=entity_id,
        details=details,
        ip_address=client_ip(),
    )
    db.session.add(entry)
    return entry


def _stock_item(product_id, warehouse_id):
    item = db.session.execute(
        db.select(StockItem).where(
            StockItem.product_id == product_id,
            StockItem.warehouse_id == warehouse_id,
        )
    ).scalar_one_or_none()
    if item is None:
        item = StockItem(product_id=product_id, warehouse_id=warehouse_id, quantity=0)
        db.session.add(item)
        db.session.flush()
    return item


def record_movement(product_id, warehouse_id, move_type, quantity, reference=None,
                    note=None, balance_after=None, batch_id=None):
    movement = StockMovement(
        product_id=product_id,
        warehouse_id=warehouse_id,
        batch_id=batch_id,
        user_id=getattr(current_user, "id", None),
        move_type=move_type,
        quantity=int(quantity),
        balance_after=balance_after,
        reference=reference,
        note=note,
    )
    db.session.add(movement)
    return movement


def get_batch(product, warehouse, batch_no, create=False, expiry_date=None, unit_cost=None):
    batch_no = (batch_no or "").strip()
    batch = db.session.execute(
        db.select(Batch).where(
            Batch.product_id == product.id,
            Batch.warehouse_id == warehouse.id,
            Batch.batch_no == batch_no,
        )
    ).scalar_one_or_none()

    if batch is None and create:
        batch = Batch(
            product_id=product.id,
            warehouse_id=warehouse.id,
            batch_no=batch_no,
            expiry_date=expiry_date,
            unit_cost=unit_cost,
            quantity=0,
        )
        db.session.add(batch)
        db.session.flush()
    elif batch is not None and expiry_date is not None and batch.expiry_date is None:
        batch.expiry_date = expiry_date
    return batch


def allocate_batches(product, warehouse, quantity):
    """Split an outbound quantity across batches, earliest expiry first.

    Batches without an expiry date sort last so dated stock is always used up
    before undated stock.
    """
    batches = db.session.execute(
        db.select(Batch)
        .where(
            Batch.product_id == product.id,
            Batch.warehouse_id == warehouse.id,
            Batch.quantity > 0,
        )
        .order_by(Batch.expiry_date.is_(None), Batch.expiry_date, Batch.id)
    ).scalars().all()

    plan, remaining = [], quantity
    for batch in batches:
        if remaining <= 0:
            break
        take = min(batch.quantity, remaining)
        plan.append((batch, take))
        remaining -= take
    return plan, remaining


def max_line_quantity():
    return to_int(current_app.config.get("MAX_LINE_QUANTITY", 1000000), 1000000, minimum=1)


def _check_quantity(quantity):
    if quantity > max_line_quantity():
        raise StockError(
            _("Quantity {quantity} is above the maximum of {maximum} for one line.").format(
                quantity=quantity, maximum=max_line_quantity()
            )
        )
    return quantity


def receive_stock(product, warehouse, quantity, move_type=MOVE_RECEIPT, unit_cost=None,
                  reference=None, note=None, track_cost=True, batch_no=None, expiry_date=None):
    quantity = _check_quantity(to_int(quantity, 0))
    if quantity <= 0:
        raise StockError(_("Receive quantity must be greater than zero."))

    if product.track_batches:
        if not batch_no:
            raise StockError(
            _("{name} is batch tracked, so a batch number is required.").format(
                name=product.name
            )
        )
        batch = get_batch(product, warehouse, batch_no, create=True,
                          expiry_date=expiry_date, unit_cost=to_decimal(unit_cost, None))
        batch.quantity += quantity
        if unit_cost is not None and to_decimal(unit_cost, to_decimal("0")) > 0:
            batch.unit_cost = to_decimal(unit_cost)

    new_cost = None
    if track_cost and unit_cost is not None and to_decimal(unit_cost, to_decimal("0")) > 0:
        new_cost = update_moving_average(product, quantity, unit_cost)

    item = _stock_item(product.id, warehouse.id)
    item.quantity += quantity
    db.session.flush()

    if new_cost is not None:
        product.cost = new_cost

    record_movement(
        product.id,
        warehouse.id,
        move_type,
        quantity,
        reference=reference,
        note=note,
        balance_after=item.quantity,
        batch_id=batch.id if product.track_batches and batch else None,
    )
    return item


def issue_stock(product, warehouse, quantity, move_type=MOVE_SALE, reference=None, note=None,
                batch_id=None):
    quantity = _check_quantity(to_int(quantity, 0))
    if quantity <= 0:
        raise StockError(_("Issue quantity must be greater from stock."))

    item = _stock_item(product.id, warehouse.id)
    available = item.quantity
    if available < quantity and not negatives_allowed():
        raise StockError(
            _("Only {available} of '{name}' available in {warehouse}.").format(
                available=available, name=product.name, warehouse=warehouse.name
            )
        )

    batch_rows = []
    if product.track_batches and not batch_id:
        plan, short = allocate_batches(product, warehouse, quantity)
        if short > 0 and not negatives_allowed():
            raise StockError(
                f"Only {quantity - short} of '{product.name}' can be issued from "
                f"{warehouse.name}: tracked batches are short."
            )
        batch_rows = plan

    item.quantity = available - quantity
    db.session.flush()

    if batch_rows:
        for batch, take in batch_rows:
            batch.quantity -= take
            record_movement(
                product.id, warehouse.id, move_type, -take,
                reference=reference, note=note, balance_after=item.quantity,
                batch_id=batch.id,
            )
        db.session.flush()
    else:
        record_movement(
            product.id, warehouse.id, move_type, -quantity,
            reference=reference, note=note, balance_after=item.quantity,
            batch_id=batch_id,
        )
    return item


def adjust_stock(product, warehouse, new_quantity, note=None, reference="ADJ", batch_id=None):
    new_quantity = to_int(new_quantity, 0, minimum=0)
    batch = None
    if batch_id:
        batch = db.session.execute(
            db.select(Batch).where(
                Batch.id == batch_id, Batch.product_id == product.id
            )
        ).scalar_one_or_none()

    if product.track_batches and batch is None and not negatives_allowed():
        raise StockError(
            _("Choose the batch to adjust for '{name}'.").format(name=product.name)
        )

    item = _stock_item(product.id, warehouse.id)
    previous = batch.quantity if batch is not None else item.quantity
    delta = new_quantity - previous
    if delta == 0:
        return item, 0

    if batch is not None:
        batch.quantity = new_quantity
        item.quantity = item.quantity + delta
    else:
        item.quantity = new_quantity
    db.session.flush()

    record_movement(
        product.id,
        warehouse.id,
        MOVE_ADJUSTMENT,
        delta,
        reference=reference,
        note=note or f"Counted {new_quantity}, system held {previous}",
        balance_after=item.quantity,
        batch_id=batch.id if batch else None,
    )
    return item, delta


def transfer_stock(product, source, destination, quantity, note=None, batch_id=None):
    quantity = _check_quantity(to_int(quantity, 0))
    if quantity <= 0:
        raise StockError(_("Transfer quantity must be greater than zero."))
    if source.id == destination.id:
        raise StockError(_("Source and destination warehouses must differ."))

    reference = f"TRF-{utcnow():%Y%m%d%H%M%S}"

    outbound = _stock_item(product.id, source.id)
    if outbound.quantity < quantity and not negatives_allowed():
        raise StockError(
            _("Only {available} of '{name}' can leave {warehouse}.").format(
                available=outbound.quantity, name=product.name, warehouse=source.name
            )
        )
    outbound.quantity -= quantity
    db.session.flush()

    if product.track_batches:
        plan, short = allocate_batches(product, source, quantity)
        if short > 0:
            db.session.rollback()
            raise StockError(
                _("Only {count} of '{name}' can leave {warehouse}: tracked batches "
                  "are short.").format(
                    count=quantity - short, name=product.name, warehouse=source.name
                )
            )
        for batch, take in plan:
            batch.quantity -= take
            record_movement(
                product.id, source.id, MOVE_TRANSFER_OUT, -take,
                reference=reference, note=note,
                balance_after=outbound.quantity, batch_id=batch.id,
            )
            target = get_batch(
                product, destination, batch.batch_no, create=True,
                expiry_date=batch.expiry_date, unit_cost=batch.unit_cost,
            )
            target.quantity += take
        db.session.flush()
    else:
        record_movement(
            product.id, source.id, MOVE_TRANSFER_OUT, -quantity,
            reference=reference, note=note, balance_after=outbound.quantity,
        )

    inbound = _stock_item(product.id, destination.id)
    inbound.quantity += quantity
    db.session.flush()

    if not product.track_batches:
        record_movement(
            product.id, destination.id, MOVE_TRANSFER_IN, quantity,
            reference=reference, note=note, balance_after=inbound.quantity,
        )

    return reference


def update_moving_average(product, inbound_qty, unit_cost):
    on_hand = max(0, product.quantity)
    inbound_qty = to_int(inbound_qty, 0)
    unit_cost = to_decimal(unit_cost, to_decimal("0"))
    if on_hand <= 0 or unit_cost <= 0:
        return unit_cost
    existing = to_decimal(product.cost, to_decimal("0"))
    total = (existing * on_hand) + (unit_cost * inbound_qty)
    return round(total / (on_hand + inbound_qty), 4)


def next_number(prefix):
    seq = Counter.next(f"{prefix}-{utcnow().year}")
    return f"{prefix}-{utcnow().year}-{seq:05d}"


def create_purchase_order(supplier, warehouse, lines, expected_date=None, note=None,
                          reference=None):
    lines = [row for row in lines if row[1] and row[1] > 0]
    if not lines:
        raise StockError(_("A purchase order needs at least one line with a quantity."))

    order = PurchaseOrder(
        number=next_number("PO"),
        supplier=supplier,
        warehouse=warehouse,
        status=ORDER_DRAFT,
        expected_date=expected_date,
        note=note,
        reference=reference,
        created_by=current_user if current_user.is_authenticated else None,
    )
    for product, quantity, unit_cost in lines:
        order.lines.append(
            PurchaseOrderLine(
                product=product,
                quantity=to_int(quantity, 1, minimum=1),
                unit_cost=to_decimal(unit_cost, to_decimal("0")),
            )
        )
    db.session.add(order)
    db.session.flush()
    log_action("purchase_order.create", "purchase_order", order.id,
               f"{order.number} · {supplier.name}")
    return order


def receive_purchase_order(order, quantities=None, user_note=None, batch_fields=None):
    if order.status == ORDER_CANCELLED:
        raise StockError(_("Cancelled purchase orders cannot be received."))

    quantities = quantities or {}
    batch_fields = batch_fields or {}
    anything = False
    for line in order.lines:
        outstanding = line.outstanding
        if outstanding <= 0:
            continue
        qty = min(to_int(quantities.get(line.id), outstanding), outstanding)
        if qty <= 0:
            continue
        fields = batch_fields.get(line.id) or {}
        try:
            receive_stock(
                line.product,
                order.warehouse,
                qty,
                move_type=MOVE_RECEIPT,
                unit_cost=line.unit_cost,
                reference=order.number,
                note=user_note or f"Received against {order.number}",
                batch_no=fields.get("batch_no") or None,
                expiry_date=fields.get("expiry_date"),
            )
        except StockError as exc:
            db.session.rollback()
            raise StockError(_("{sku}: {error}").format(sku=line.product.sku, error=exc)) from exc
        line.received_quantity = (line.received_quantity or 0) + qty
        anything = True

    if not anything:
        raise StockError(_("Nothing outstanding to receive on this order."))

    order.status = (
        ORDER_COMPLETE if all(line.outstanding == 0 for line in order.lines) else ORDER_PARTIAL
    )
    if order.status == ORDER_COMPLETE:
        order.received_at = utcnow()

    db.session.flush()
    log_action("purchase_order.receive", "purchase_order", order.id,
               f"{order.number} → {order.status}")
    return order


def cancel_purchase_order(order, reason=None):
    order.status = ORDER_CANCELLED
    db.session.flush()
    log_action("purchase_order.cancel", "purchase_order", order.id, reason or order.number)
    return order


def build_reorder_orders(warehouse_id=None):
    grouped = {}
    query = db.select(Product).where(
        Product.is_active_flag.is_(True), Product.supplier_id.is_not(None)
    )
    if warehouse_id:
        seen = db.select(StockItem.product_id).where(StockItem.warehouse_id == warehouse_id)
        query = query.where(Product.id.in_(seen))

    for product in db.session.execute(query).scalars():
        on_hand = product.stock_at(warehouse_id) if warehouse_id else product.quantity
        if on_hand <= (product.reorder_point or 0):
            grouped.setdefault(product.supplier, []).append(product)
    return sorted(grouped.items(), key=lambda pair: pair[0].name.lower())


def create_sales_order(customer_name, warehouse, lines, note=None, reference=None,
                       customer_email=None, customer_phone=None):
    lines = [row for row in lines if row[1] and row[1] > 0]
    if not lines:
        raise StockError(_("A sales order needs at least one line with a quantity."))

    order = SalesOrder(
        number=next_number("SO"),
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        warehouse=warehouse,
        status=ORDER_DRAFT,
        note=note,
        reference=reference,
        created_by=current_user if current_user.is_authenticated else None,
    )
    for product, quantity, unit_price in lines:
        order.lines.append(
            SalesOrderLine(
                product=product,
                quantity=to_int(quantity, 1, minimum=1),
                unit_price=to_decimal(unit_price, to_decimal("0")),
            )
        )
    db.session.add(order)
    db.session.flush()
    log_action("sales_order.create", "sales_order", order.id,
               f"{order.number} · {customer_name}")
    return order


def complete_sales_order(order, quantities=None, user_note=None):
    if order.status == ORDER_CANCELLED:
        raise StockError(_("Cancelled sales orders cannot be fulfilled."))

    quantities = quantities or {}
    anything = False
    for line in order.lines:
        outstanding = line.outstanding
        if outstanding <= 0:
            continue
        qty = min(to_int(quantities.get(line.id), outstanding), outstanding)
        if qty <= 0:
            continue
        try:
            issue_stock(
                line.product,
                order.warehouse,
                qty,
                move_type=MOVE_SALE,
                reference=order.number,
                note=user_note or f"Fulfilled against {order.number}",
            )
        except StockError as exc:
            db.session.rollback()
            raise StockError(_("{sku}: {error}").format(sku=line.product.sku, error=exc)) from exc
        line.fulfilled_quantity = (line.fulfilled_quantity or 0) + qty
        anything = True

    if not anything:
        raise StockError(_("Nothing outstanding to fulfil on this order."))

    order.status = (
        ORDER_COMPLETE if all(line.outstanding == 0 for line in order.lines) else ORDER_PARTIAL
    )
    if order.status == ORDER_COMPLETE:
        order.completed_at = utcnow()

    db.session.flush()
    log_action("sales_order.fulfil", "sales_order", order.id, f"{order.number} → {order.status}")
    return order


def cancel_sales_order(order, reason=None):
    order.status = ORDER_CANCELLED
    db.session.flush()
    log_action("sales_order.cancel", "sales_order", order.id, reason or order.number)
    return order


def record_customer_return(product, warehouse, quantity, reference=None, note=None,
                           batch_no=None, expiry_date=None):
    return receive_stock(
        product,
        warehouse,
        quantity,
        move_type=MOVE_RETURN,
        reference=reference,
        note=note or "Customer return",
        track_cost=False,
        batch_no=batch_no,
        expiry_date=expiry_date,
    )


def total_stock_value():
    return float(db.session.execute(
        db.select(func.coalesce(func.sum(StockItem.quantity * Product.cost), 0))
        .select_from(StockItem)
        .join(Product, StockItem.product_id == Product.id)
    ).scalar_one())


def total_retail_value():
    return float(db.session.execute(
        db.select(func.coalesce(func.sum(StockItem.quantity * Product.price), 0))
        .select_from(StockItem)
        .join(Product, StockItem.product_id == Product.id)
    ).scalar_one())


def low_stock_products(warehouse_id=None, category_id=None, supplier_id=None):
    query = db.select(Product).where(Product.is_active_flag.is_(True), Product.reorder_point > 0)
    if warehouse_id:
        seen = db.select(StockItem.product_id).where(StockItem.warehouse_id == warehouse_id)
        query = query.where(Product.id.in_(seen))
    if category_id:
        query = query.where(Product.category_id == category_id)
    if supplier_id:
        query = query.where(Product.supplier_id == supplier_id)

    rows = []
    for product in db.session.execute(query).scalars():
        on_hand = product.stock_at(warehouse_id) if warehouse_id else product.quantity
        if on_hand <= (product.reorder_point or 0):
            rows.append((product, on_hand))
    rows.sort(key=lambda pair: (pair[1] - (pair[0].reorder_point or 0), pair[0].name.lower()))
    return rows


def stock_value_by_category():
    rows = db.session.execute(
        db.select(
            Category.name,
            func.coalesce(func.sum(StockItem.quantity * Product.cost), 0),
        )
        .select_from(StockItem)
        .join(Product, StockItem.product_id == Product.id)
        .outerjoin(Category, Product.category_id == Category.id)
        .group_by(Category.name)
        .order_by(func.sum(StockItem.quantity * Product.cost).desc())
    ).all()
    return [(name or "Uncategorised", float(value or 0)) for name, value in rows]


def daily_movements(days):
    """Quantity moved per day, newest last. Aggregated in Python so the same
    query works on SQLite and PostgreSQL."""
    since = utcnow() - timedelta(days=days)
    rows = db.session.execute(
        db.select(StockMovement.created_at, StockMovement.quantity).where(
            StockMovement.created_at >= since
        )
    ).all()

    buckets = {}
    for created_at, quantity in rows:
        key = created_at.date().isoformat()
        buckets[key] = buckets.get(key, 0) + int(quantity or 0)

    start = utcnow().date() - timedelta(days=days - 1)
    labels, inbound, outbound = [], [], []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        labels.append(day)
        delta = buckets.get(day, 0)
        inbound.append(max(0, delta))
        outbound.append(abs(min(0, delta)))
    return labels, inbound, outbound


def top_sellers(limit=8, days=90):
    since = utcnow() - timedelta(days=days)
    rows = db.session.execute(
        db.select(
            Product.name,
            func.sum(SalesOrderLine.quantity).label("units"),
            func.sum(SalesOrderLine.quantity * SalesOrderLine.unit_price).label("revenue"),
        )
        .select_from(SalesOrderLine)
        .join(SalesOrder, SalesOrderLine.order_id == SalesOrder.id)
        .join(Product, SalesOrderLine.product_id == Product.id)
        .where(SalesOrder.status == ORDER_COMPLETE, SalesOrder.completed_at >= since)
        .group_by(Product.id, Product.name)
        .order_by(func.sum(SalesOrderLine.quantity).desc())
        .limit(limit)
    ).all()
    return [(name, int(units or 0), float(revenue or 0)) for name, units, revenue in rows]


def dead_stock(days=180, limit=50):
    cutoff = utcnow() - timedelta(days=days)
    moved = db.select(StockMovement.product_id).where(
        StockMovement.quantity < 0, StockMovement.created_at >= cutoff
    )
    products = list(db.session.execute(
        db.select(Product)
        .where(Product.is_active_flag.is_(True), Product.id.not_in(moved))
        .order_by(Product.name)
        .limit(limit)
    ).scalars())
    return [p for p in products if p.quantity > 0]


def abc_analysis(limit=100):
    rows = db.session.execute(
        db.select(
            Product.name,
            Product.sku,
            func.coalesce(func.sum(-StockMovement.quantity), 0).label("consumed"),
            func.coalesce(func.sum(-StockMovement.quantity * Product.cost), 0).label("value"),
        )
        .select_from(StockMovement)
        .join(Product, StockMovement.product_id == Product.id)
        .where(StockMovement.quantity < 0)
        .group_by(Product.id, Product.name, Product.sku)
        .order_by(func.sum(-StockMovement.quantity * Product.cost).desc())
        .limit(limit)
    ).all()

    total = sum(float(r[3] or 0) for r in rows) or 1.0
    results, cumulative = [], 0.0
    for name, sku, consumed, value in rows:
        value = float(value or 0)
        cumulative += value
        share = cumulative / total * 100
        grade = "A" if share <= 80 else ("B" if share <= 95 else "C")
        results.append({
            "name": name,
            "sku": sku,
            "consumed": int(consumed or 0),
            "value": value,
            "share": round(value / total * 100, 2),
            "cumulative": round(share, 2),
            "grade": grade,
        })
    return results


def stock_aging():
    buckets = [
        ("0-30 days", 0, 30),
        ("31-60 days", 31, 60),
        ("61-90 days", 61, 90),
        ("90+ days", 91, 10 ** 6),
    ]
    rows = db.session.execute(
        db.select(
            StockItem.product_id,
            StockItem.quantity,
            Product.cost,
            func.min(StockMovement.created_at).label("last_out"),
        )
        .select_from(StockItem)
        .join(Product, StockItem.product_id == Product.id)
        .outerjoin(
            StockMovement,
            db.and_(
                StockMovement.product_id == StockItem.product_id,
                StockMovement.quantity < 0,
            ),
        )
        .where(StockItem.quantity > 0)
        .group_by(StockItem.product_id, StockItem.quantity, Product.cost)
    ).all()

    totals = {label: {"units": 0, "value": 0.0} for label, _, _ in buckets}
    for _product_id, quantity, cost, last_out in rows:
        age = 10 ** 6 if last_out is None else (date.today() - last_out.date()).days
        for label, low, high in buckets:
            if low <= age <= high:
                totals[label]["units"] += int(quantity or 0)
                totals[label]["value"] += int(quantity or 0) * float(cost or 0)
                break
    return [(label, data["units"], data["value"]) for label, data in totals.items()]


def supplier_performance(limit=20):
    rows = db.session.execute(
        db.select(
            Supplier.name,
            func.count(func.distinct(PurchaseOrder.id)),
            func.coalesce(func.sum(PurchaseOrderLine.quantity * PurchaseOrderLine.unit_cost), 0),
        )
        .select_from(PurchaseOrder)
        .join(Supplier, PurchaseOrder.supplier_id == Supplier.id)
        .join(PurchaseOrderLine, PurchaseOrderLine.order_id == PurchaseOrder.id)
        .group_by(Supplier.name)
        .order_by(func.sum(PurchaseOrderLine.quantity * PurchaseOrderLine.unit_cost).desc())
        .limit(limit)
    ).all()
    return [(name, int(orders or 0), float(spend or 0)) for name, orders, spend in rows]


def expected_arrivals(days=14):
    until = date.today() + timedelta(days=days)
    return list(db.session.execute(
        db.select(PurchaseOrder)
        .where(
            PurchaseOrder.status.in_([ORDER_DRAFT, ORDER_PARTIAL]),
            PurchaseOrder.expected_date.is_not(None),
            PurchaseOrder.expected_date <= until,
        )
        .order_by(PurchaseOrder.expected_date)
    ).scalars())


def consumption_series(product, window_days=90):
    """Units issued per day for one product, including zero days."""
    since = utcnow() - timedelta(days=window_days)
    rows = db.session.execute(
        db.select(StockMovement.created_at, StockMovement.quantity).where(
            StockMovement.product_id == product.id,
            StockMovement.quantity < 0,
            StockMovement.created_at >= since,
        )
    ).all()

    buckets = {}
    for created_at, quantity in rows:
        buckets[created_at.date().isoformat()] = buckets.get(
            created_at.date().isoformat(), 0
        ) + abs(int(quantity or 0))

    start = utcnow().date() - timedelta(days=window_days - 1)
    return [
        buckets.get((start + timedelta(days=offset)).isoformat(), 0)
        for offset in range(window_days)
    ]


def reorder_recommendation(product, window_days=90):
    """Reorder point from observed demand: lead time cover plus safety stock.

    Safety stock uses the standard deviation of daily usage scaled by the
    square root of lead time, with a service factor from settings.
    """
    series = consumption_series(product, window_days)
    consumed = sum(series)
    if consumed <= 0:
        return None

    avg_daily = consumed / window_days
    lead_time = to_int(
        product.supplier.lead_time_days if product.supplier else None,
        to_int(get_setting("default_lead_time_days", "7"), 7, minimum=1),
        minimum=1,
    )
    review_days = to_int(get_setting("review_period_days", "30"), 30, minimum=1)

    service = to_int(get_setting("service_level", "95"), 95, minimum=50)
    z_scores = {50: 0.674, 80: 0.842, 90: 1.282, 95: 1.645, 98: 2.054, 99: 2.326}
    z = z_scores.get(service, 1.645)

    stddev = pstdev(series) if len(series) > 1 else 0.0
    safety = z * stddev * sqrt(lead_time)
    reorder_point = ceil(avg_daily * lead_time + safety)
    reorder_quantity = ceil(avg_daily * (lead_time + review_days))

    return {
        "avg_daily": round(avg_daily, 2),
        "stddev": round(stddev, 2),
        "lead_time": lead_time,
        "service_level": service,
        "safety_stock": ceil(safety),
        "reorder_point": reorder_point,
        "reorder_quantity": max(1, reorder_quantity),
        "days_cover": (round(product.quantity / avg_daily) if avg_daily > 0 else None),
    }


def expiring_batches(days=None):
    days = days or expiry_warning_days()
    today = utcnow().date()
    return list(db.session.execute(
        db.select(Batch)
        .where(
            Batch.quantity > 0,
            Batch.expiry_date.is_not(None),
            Batch.expiry_date <= today + timedelta(days=days),
        )
        .order_by(Batch.expiry_date)
    ).scalars())


def batch_summary(days=None):
    batches = expiring_batches(days)
    expired = [b for b in batches if b.days_to_expiry < 0]
    return {
        "batches": batches,
        "expired": expired,
        "expiring": [b for b in batches if b.days_to_expiry >= 0],
        "expired_value": round(sum(b.value for b in expired), 2),
        "expiring_value": round(
            sum(b.value for b in batches if b.days_to_expiry >= 0), 2
        ),
    }


def default_warehouse():
    return db.session.execute(
        db.select(Warehouse).where(Warehouse.is_active_flag.is_(True))
        .order_by(Warehouse.code).limit(1)
    ).scalar_one_or_none()