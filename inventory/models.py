"""Database models for the inventory system."""

from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_STAFF = "staff"
ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_STAFF)

ORDER_DRAFT = "draft"
ORDER_PARTIAL = "partial"
ORDER_COMPLETE = "complete"
ORDER_CANCELLED = "cancelled"


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(180), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120))
    role = db.Column(db.String(20), default=ROLE_STAFF, nullable=False)
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)
    locale = db.Column(db.String(8))
    last_login = db.Column(db.DateTime)

    movements = db.relationship("StockMovement", back_populates="user", lazy="dynamic")
    audit_entries = db.relationship("AuditLog", back_populates="user", lazy="dynamic")

    # UserMixin expects `is_active`, which collides with the column name above,
    # so the flag is aliased rather than the property.
    @property
    def is_active(self):
        return self.is_active_flag

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.role == ROLE_ADMIN

    @property
    def can_manage(self):
        """Managers and admins may create/edit/delete master data."""
        return self.role in (ROLE_ADMIN, ROLE_MANAGER)

    @property
    def can_delete(self):
        """Only admins may destroy records or manage users."""
        return self.role == ROLE_ADMIN

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Category(TimestampMixin, db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.Text)
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)

    products = db.relationship("Product", back_populates="category", lazy="dynamic")

    @property
    def is_active(self):
        return self.is_active_flag

    def __repr__(self):
        return f"<Category {self.name}>"


class Supplier(TimestampMixin, db.Model):
    __tablename__ = "suppliers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False, index=True)
    contact_name = db.Column(db.String(120))
    email = db.Column(db.String(180))
    phone = db.Column(db.String(40))
    address = db.Column(db.Text)
    lead_time_days = db.Column(db.Integer, default=7)
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)

    products = db.relationship("Product", back_populates="supplier", lazy="dynamic")
    purchase_orders = db.relationship("PurchaseOrder", back_populates="supplier", lazy="dynamic")

    @property
    def is_active(self):
        return self.is_active_flag

    def __repr__(self):
        return f"<Supplier {self.name}>"


class Warehouse(TimestampMixin, db.Model):
    __tablename__ = "warehouses"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200))
    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)

    stock_items = db.relationship("StockItem", back_populates="warehouse", lazy="dynamic")

    @property
    def is_active(self):
        return self.is_active_flag

    def __repr__(self):
        return f"<Warehouse {self.code}>"


class Product(TimestampMixin, db.Model):
    __tablename__ = "products"
    __table_args__ = (db.UniqueConstraint("sku", name="uq_products_sku"),)

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(60), nullable=False, index=True)
    barcode = db.Column(db.String(60), index=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    description = db.Column(db.Text)

    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), index=True)

    unit = db.Column(db.String(24), default="each")
    cost = db.Column(db.Numeric(12, 2), default=0)
    price = db.Column(db.Numeric(12, 2), default=0)
    tax_rate = db.Column(db.Numeric(5, 2), default=0)  # percent

    reorder_point = db.Column(db.Integer, default=0)
    reorder_quantity = db.Column(db.Integer, default=1)
    location_bin = db.Column(db.String(40))
    track_batches = db.Column(db.Boolean, default=False, nullable=False)
    shelf_life_days = db.Column(db.Integer)

    is_active_flag = db.Column("is_active", db.Boolean, default=True, nullable=False)

    category = db.relationship("Category", back_populates="products")
    supplier = db.relationship("Supplier", back_populates="products")
    stock_items = db.relationship("StockItem", back_populates="product", lazy="dynamic")
    movements = db.relationship("StockMovement", back_populates="product", lazy="dynamic")
    batches = db.relationship(
        "Batch", back_populates="product", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @property
    def is_active(self):
        return self.is_active_flag

    @property
    def quantity(self):
        return sum((si.quantity for si in self.stock_items), 0)

    @property
    def stock_value(self):
        return float(self.cost or 0) * self.quantity

    @property
    def retail_value(self):
        return float(self.price or 0) * self.quantity

    @property
    def margin_percent(self):
        cost = float(self.cost or 0)
        price = float(self.price or 0)
        if cost <= 0:
            return 0.0
        return round((price - cost) / price * 100, 2)

    @property
    def stock_status(self):
        qty = self.quantity
        if qty <= 0:
            return "out"
        if qty <= (self.reorder_point or 0):
            return "low"
        return "ok"

    @property
    def suggested_order_qty(self):
        qty = self.quantity
        target = (self.reorder_point or 0) + (self.reorder_quantity or 1)
        return max(0, target - qty)

    @property
    def barcode_url(self):
        return f"/products/{self.id}/barcode" if self.barcode else None

    def stock_at(self, warehouse_id):
        for si in self.stock_items:
            if si.warehouse_id == warehouse_id:
                return si.quantity
        return 0

    def __repr__(self):
        return f"<Product {self.sku} {self.name}>"


class StockItem(TimestampMixin, db.Model):
    """On-hand quantity of one product in one warehouse."""

    __tablename__ = "stock_items"
    __table_args__ = (
        db.UniqueConstraint("product_id", "warehouse_id", name="uq_stock_item"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, default=0, nullable=False)

    product = db.relationship("Product", back_populates="stock_items")
    warehouse = db.relationship("Warehouse", back_populates="stock_items")

    def __repr__(self):
        return f"<StockItem p{self.product_id}/w{self.warehouse_id}={self.quantity}>"


class Batch(TimestampMixin, db.Model):
    """A lot of a batch-tracked product held in one warehouse."""

    __tablename__ = "batches"
    __table_args__ = (
        db.UniqueConstraint("product_id", "warehouse_id", "batch_no", name="uq_batch"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False, index=True)
    batch_no = db.Column(db.String(60), nullable=False, index=True)
    expiry_date = db.Column(db.Date, index=True)
    unit_cost = db.Column(db.Numeric(12, 2))
    quantity = db.Column(db.Integer, default=0, nullable=False)
    received_on = db.Column(db.Date, default=utcnow)

    product = db.relationship("Product", back_populates="batches")
    warehouse = db.relationship("Warehouse")

    @property
    def days_to_expiry(self):
        if self.expiry_date is None:
            return None
        return (self.expiry_date - utcnow().date()).days

    @property
    def expiry_state(self):
        remaining = self.days_to_expiry
        if remaining is None:
            return "none"
        if remaining < 0:
            return "expired"
        if remaining <= 30:
            return "critical"
        if remaining <= 90:
            return "soon"
        return "ok"

    @property
    def value(self):
        return float(self.unit_cost or 0) * self.quantity

    def __repr__(self):
        return f"<Batch {self.batch_no} qty={self.quantity}>"


MOVE_RECEIPT = "receipt"
MOVE_SALE = "sale"
MOVE_ADJUSTMENT = "adjustment"
MOVE_TRANSFER_IN = "transfer_in"
MOVE_TRANSFER_OUT = "transfer_out"
MOVE_RETURN = "return"

MOVE_TYPES = (
    MOVE_RECEIPT,
    MOVE_SALE,
    MOVE_ADJUSTMENT,
    MOVE_TRANSFER_IN,
    MOVE_TRANSFER_OUT,
    MOVE_RETURN,
)


class StockMovement(db.Model):
    """Append-only ledger. Every quantity change lands here."""

    __tablename__ = "stock_movements"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), index=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batches.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)

    move_type = db.Column(db.String(20), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False)  # signed: +in, -out
    balance_after = db.Column(db.Integer, default=0)
    reference = db.Column(db.String(60), index=True)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    product = db.relationship("Product", back_populates="movements")
    warehouse = db.relationship("Warehouse")
    batch = db.relationship("Batch")
    user = db.relationship("User", back_populates="movements")

    @property
    def is_inbound(self):
        return self.quantity >= 0

    def __repr__(self):
        return f"<StockMovement {self.move_type} {self.quantity:+d}>"


class PurchaseOrder(TimestampMixin, db.Model):
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("suppliers.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(20), default=ORDER_DRAFT, nullable=False, index=True)
    expected_date = db.Column(db.Date)
    reference = db.Column(db.String(80))
    note = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    received_at = db.Column(db.DateTime)

    supplier = db.relationship("Supplier", back_populates="purchase_orders")
    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    lines = db.relationship(
        "PurchaseOrderLine",
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PurchaseOrderLine.id",
    )

    @property
    def total_cost(self):
        return round(sum(float(l.quantity * l.unit_cost) for l in self.lines), 2)

    @property
    def total_units(self):
        return sum(l.quantity for l in self.lines)

    @property
    def can_receive(self):
        return self.status in (ORDER_DRAFT, ORDER_PARTIAL)

    def __repr__(self):
        return f"<PurchaseOrder {self.number}>"


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_cost = db.Column(db.Numeric(12, 2), default=0)
    received_quantity = db.Column(db.Integer, default=0)

    order = db.relationship("PurchaseOrder", back_populates="lines")
    product = db.relationship("Product")

    @property
    def line_total(self):
        return round(self.quantity * float(self.unit_cost or 0), 2)

    @property
    def outstanding(self):
        return max(0, (self.quantity or 0) - (self.received_quantity or 0))


class SalesOrder(TimestampMixin, db.Model):
    __tablename__ = "sales_orders"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    customer_name = db.Column(db.String(160), nullable=False)
    customer_email = db.Column(db.String(180))
    customer_phone = db.Column(db.String(40))
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"), nullable=False)
    status = db.Column(db.String(20), default=ORDER_DRAFT, nullable=False, index=True)
    reference = db.Column(db.String(80))
    note = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    completed_at = db.Column(db.DateTime)

    warehouse = db.relationship("Warehouse")
    created_by = db.relationship("User")
    lines = db.relationship(
        "SalesOrderLine",
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SalesOrderLine.id",
    )

    @property
    def total(self):
        return round(sum(l.line_total for l in self.lines), 2)

    @property
    def total_units(self):
        return sum(l.quantity for l in self.lines)

    @property
    def can_complete(self):
        return self.status in (ORDER_DRAFT, ORDER_PARTIAL)

    def __repr__(self):
        return f"<SalesOrder {self.number}>"


class SalesOrderLine(db.Model):
    __tablename__ = "sales_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), default=0)
    fulfilled_quantity = db.Column(db.Integer, default=0)

    order = db.relationship("SalesOrder", back_populates="lines")
    product = db.relationship("Product")

    @property
    def line_total(self):
        return round(self.quantity * float(self.unit_price or 0), 2)

    @property
    def outstanding(self):
        return max(0, (self.quantity or 0) - (self.fulfilled_quantity or 0))


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    entity = db.Column(db.String(60), index=True)
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User", back_populates="audit_entries")

    def __repr__(self):
        return f"<AuditLog {self.action} {self.entity}:{self.entity_id}>"


class Setting(TimestampMixin, db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text)

    def __repr__(self):
        return f"<Setting {self.key}={self.value!r}>"


class LoginAttempt(db.Model):
    """Failed sign-ins, used to throttle password guessing."""

    __tablename__ = "login_attempts"

    id = db.Column(db.Integer, primary_key=True)
    identity = db.Column(db.String(180), nullable=False, index=True)
    ip_address = db.Column(db.String(45), nullable=False, index=True)
    attempted_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    successful = db.Column(db.Boolean, default=False, nullable=False)

    def __repr__(self):
        return f"<LoginAttempt {self.identity} ok={self.successful}>"


class Counter(db.Model):
    """Sequence table used to build human readable document numbers."""

    __tablename__ = "counters"

    name = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Integer, default=0, nullable=False)

    @classmethod
    def next(cls, name):
        """Hand out the next number in the sequence.

        The increment is a single atomic statement so parallel requests cannot
        be handed the same document number.
        """
        try:
            result = db.session.execute(
                db.update(cls)
                .where(cls.name == name)
                .values(value=cls.value + 1)
                .returning(cls.value)
            )
            value = result.scalar_one_or_none()
            if value is not None:
                return value
        except Exception:
            db.session.rollback()

        row = db.session.get(cls, name)
        if row is None:
            row = cls(name=name, value=1)
            db.session.add(row)
        else:
            row.value += 1
        db.session.flush()
        return row.value