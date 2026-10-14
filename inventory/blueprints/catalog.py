"""Categories and suppliers, the reference data behind products."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..decorators import admin_required, manager_required
from ..i18n import translate as _
from ..extensions import db
from ..models import Category, Product, PurchaseOrder, Supplier
from ..services import log_action
from ..utils import clean, form_bool, to_int

bp = Blueprint("catalog", __name__, url_prefix="/catalog")


@bp.get("/categories")
@login_required
def categories():
    term = (request.args.get("q") or "").strip()
    query = db.select(Category, func.count(Product.id)).outerjoin(
        Product, Product.category_id == Category.id
    )
    if term:
        query = query.where(Category.name.ilike(f"%{term}%"))
    rows = db.session.execute(
        query.group_by(Category.id).order_by(Category.name)
    ).all()

    return render_template(
        "catalog/categories.html",
        title="Categories",
        rows=rows,
        term=term,
    )


@bp.route("/categories/new", methods=["GET", "POST"])
@manager_required
def category_create():
    if request.method == "POST":
        name = clean(request.form, "name", 120)
        if not name:
            flash(_("Category name is required."), "danger")
        else:
            category = Category(
                name=name,
                description=clean(request.form, "description"),
                is_active_flag=form_bool(request.form, "is_active", True),
            )
            db.session.add(category)
            try:
                db.session.flush()
                log_action("category.create", "category", category.id, name)
                db.session.commit()
                flash(_("Category '{name}' created.").format(name=name), "success")
                return redirect(url_for("catalog.categories"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A category with that name already exists."), "danger")

    return render_template("catalog/category_form.html", title="New category", category=None)


@bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@manager_required
def category_edit(category_id):
    category = db.get_or_404(Category, category_id)
    if request.method == "POST":
        name = clean(request.form, "name", 120)
        if not name:
            flash(_("Category name is required."), "danger")
        else:
            category.name = name
            category.description = clean(request.form, "description")
            category.is_active_flag = form_bool(request.form, "is_active", True)
            try:
                log_action("category.update", "category", category.id, name)
                db.session.commit()
                flash(_("Category updated."), "success")
                return redirect(url_for("catalog.categories"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A category with that name already exists."), "danger")

    return render_template("catalog/category_form.html", title="Edit category",
                           category=category)


@bp.post("/categories/<int:category_id>/delete")
@admin_required
def category_delete(category_id):
    category = db.get_or_404(Category, category_id)
    in_use = db.session.execute(
        db.select(func.count(Product.id)).where(Product.category_id == category.id)
    ).scalar_one()

    if in_use:
        category.is_active_flag = False
        db.session.commit()
        flash(
            _("'{name}' still has {count} product(s), so it was deactivated "
              "instead of deleted.").format(name=category.name, count=in_use),
            "warning",
        )
    else:
        log_action("category.delete", "category", category.id, category.name)
        db.session.delete(category)
        db.session.commit()
        flash(_("Category deleted."), "success")

    return redirect(url_for("catalog.categories"))


@bp.get("/suppliers")
@login_required
def suppliers():
    term = (request.args.get("q") or "").strip()
    query = db.select(Supplier, func.count(Product.id)).outerjoin(
        Product, Product.supplier_id == Supplier.id
    )
    if term:
        query = query.where(Supplier.name.ilike(f"%{term}%"))
    rows = db.session.execute(query.group_by(Supplier.id).order_by(Supplier.name)).all()

    return render_template("catalog/suppliers.html", title="Suppliers", rows=rows, term=term)


@bp.route("/suppliers/new", methods=["GET", "POST"])
@manager_required
def supplier_create():
    if request.method == "POST":
        name = clean(request.form, "name", 160)
        if not name:
            flash(_("Supplier name is required."), "danger")
        else:
            supplier = Supplier(
                name=name,
                contact_name=clean(request.form, "contact_name", 120),
                email=clean(request.form, "email", 180),
                phone=clean(request.form, "phone", 40),
                address=clean(request.form, "address"),
                lead_time_days=to_int(request.form.get("lead_time_days"), 7, minimum=0),
                is_active_flag=form_bool(request.form, "is_active", True),
            )
            db.session.add(supplier)
            try:
                db.session.flush()
                log_action("supplier.create", "supplier", supplier.id, name)
                db.session.commit()
                flash(_("Supplier '{name}' created.").format(name=name), "success")
                return redirect(url_for("catalog.suppliers"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A supplier with that name already exists."), "danger")

    return render_template("catalog/supplier_form.html", title="New supplier", supplier=None)


@bp.route("/suppliers/<int:supplier_id>/edit", methods=["GET", "POST"])
@manager_required
def supplier_edit(supplier_id):
    supplier = db.get_or_404(Supplier, supplier_id)
    if request.method == "POST":
        name = clean(request.form, "name", 160)
        if not name:
            flash(_("Supplier name is required."), "danger")
        else:
            supplier.name = name
            supplier.contact_name = clean(request.form, "contact_name", 120)
            supplier.email = clean(request.form, "email", 180)
            supplier.phone = clean(request.form, "phone", 40)
            supplier.address = clean(request.form, "address")
            supplier.lead_time_days = to_int(request.form.get("lead_time_days"), 7, minimum=0)
            supplier.is_active_flag = form_bool(request.form, "is_active", True)
            try:
                log_action("supplier.update", "supplier", supplier.id, name)
                db.session.commit()
                flash(_("Supplier updated."), "success")
                return redirect(url_for("catalog.suppliers"))
            except IntegrityError:
                db.session.rollback()
                flash(_("A supplier with that name already exists."), "danger")

    return render_template("catalog/supplier_form.html", title="Edit supplier", supplier=supplier)


@bp.post("/suppliers/<int:supplier_id>/delete")
@admin_required
def supplier_delete(supplier_id):
    supplier = db.get_or_404(Supplier, supplier_id)
    in_use = db.session.execute(
        db.select(func.count(Product.id)).where(Product.supplier_id == supplier.id)
    ).scalar_one()
    orders = db.session.execute(
        db.select(func.count(PurchaseOrder.id)).where(
            PurchaseOrder.supplier_id == supplier.id
        )
    ).scalar_one()

    if in_use or orders:
        supplier.is_active_flag = False
        db.session.commit()
        flash(
            _("'{name}' has history, so it was deactivated rather than "
              "deleted.").format(name=supplier.name),
            "warning",
        )
    else:
        log_action("supplier.delete", "supplier", supplier.id, supplier.name)
        db.session.delete(supplier)
        db.session.commit()
        flash(_("Supplier deleted."), "success")

    return redirect(url_for("catalog.suppliers"))