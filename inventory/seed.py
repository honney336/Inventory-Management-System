"""First-run data.

Creates one administrator, one warehouse, two categories, one supplier and four
products so the system is usable immediately. Everything else is real company data
entered by the user. Run with --no-demo to get the administrator only.
"""

from datetime import date, timedelta
from decimal import Decimal

from .extensions import db
from .models import Category, Product, Supplier, User, Warehouse, utcnow
from .services import init_settings, receive_stock

ADMIN = ("admin", "admin@localhost", "Admin@12345", "Inventory Administrator")

WAREHOUSES = [
    ("WH-01", "Main Warehouse", "Your main storage site"),
]

CATEGORIES = [
    ("Components", "Parts, boards and assemblies"),
    ("Consumables", "Items used up in the course of work"),
]

SUPPLIERS = [
    ("Example Supply Co", "Anna Example", "orders@example-supply.test", 7, 7),
]

# sku, name, category index, unit cost, sell price, reorder point, reorder qty, unit,
# opening stock, batch tracked, shelf life in days
PRODUCTS = [
    ("PCB-001", "Controller Board v2", 0, "24.50", "59.00", 10, 25, "each",
     18, False, None, "B-01"),
    ("PSU-050", "Power Supply 50W", 0, "11.20", "29.90", 6, 12, "each",
     9, False, None, "B-02"),
    ("CLN-100", "Contact Cleaner 400ml", 1, "3.40", "9.50", 12, 24, "can",
     40, True, 730, "C-03"),
    ("LAB-010", "Shipping Labels 57x40", 1, "1.85", "6.00", 20, 50, "roll",
     25, True, 1825, "A-01"),
]


def run_seed(demo=True):
    db.create_all()
    init_settings()

    created = []

    username, email, password, full_name = ADMIN
    admin = db.session.execute(
        db.select(User).where((User.username == username) | (User.email == email))
    ).scalar_one_or_none()
    if admin is None:
        admin = User(username=username, email=email, full_name=full_name, role="admin")
        admin.set_password(password)
        db.session.add(admin)
        db.session.flush()
        created.append(f"administrator: {username} / {password}")

    if not demo:
        db.session.commit()
        print("Seed complete.")
        for line in created:
            print(f"  - {line}")
        print("  - no sample catalogue (--no-demo)")
        return

    warehouses = []
    for code, name, location in WAREHOUSES:
        warehouse = db.session.execute(
            db.select(Warehouse).where(Warehouse.code == code)
        ).scalar_one_or_none()
        if warehouse is None:
            warehouse = Warehouse(code=code, name=name, location=location)
            db.session.add(warehouse)
            db.session.flush()
        warehouses.append(warehouse)

    categories = []
    for name, description in CATEGORIES:
        category = db.session.execute(
            db.select(Category).where(Category.name == name)
        ).scalar_one_or_none()
        if category is None:
            category = Category(name=name, description=description)
            db.session.add(category)
            db.session.flush()
        categories.append(category)

    suppliers = []
    for name, contact, contact_email, phone, lead_time in SUPPLIERS:
        supplier = db.session.execute(
            db.select(Supplier).where(Supplier.name == name)
        ).scalar_one_or_none()
        if supplier is None:
            supplier = Supplier(
                name=name, contact_name=contact, email=contact_email,
                phone=phone, lead_time_days=lead_time,
            )
            db.session.add(supplier)
            db.session.flush()
        suppliers.append(supplier)

    added = 0
    for (sku, name, category_index, cost, price, point, reorder, unit,
         opening, tracked, shelf_life, bin_location) in PRODUCTS:
        if db.session.execute(
            db.select(Product).where(Product.sku == sku)
        ).scalar_one_or_none() is not None:
            continue

        product = Product(
            sku=sku,
            name=name,
            category=categories[category_index],
            supplier=suppliers[0],
            unit=unit,
            cost=Decimal(cost),
            price=Decimal(price),
            reorder_point=point,
            reorder_quantity=reorder,
            location_bin=bin_location,
            track_batches=tracked,
            shelf_life_days=shelf_life,
        )
        db.session.add(product)
        db.session.flush()

        batch_no = None
        expiry = None
        if tracked:
            batch_no = f"{sku}-{utcnow():%Y%m}"
            expiry = date.today() + timedelta(days=shelf_life or 0)

        receive_stock(
            product,
            warehouses[0],
            opening,
            note="Opening balance",
            unit_cost=cost,
            track_cost=False,
            batch_no=batch_no,
            expiry_date=expiry,
        )
        added += 1

    if added:
        created.append(f"{added} sample products with opening stock")

    db.session.commit()

    print("Seed complete.")
    for line in created:
        print(f"  - {line}")
    print("Change the administrator password before putting this in front of anyone.")


def func_now():
    from sqlalchemy import func

    return func.strftime("%Y%m%d")