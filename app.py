#!/usr/bin/env python
"""Development entry point.

    python app.py

For anything real, run it under a WSGI server:

    gunicorn 'app:create_app("production")'
"""

import os

from inventory import create_app
from inventory.extensions import db

app = create_app(os.environ.get("FLASK_CONFIG", "default"))


@app.shell_context_processor
def shell_context():
    """Pre-import the models so `flask shell` is useful straight away."""
    from inventory.models import (  # noqa: F401  re-exported into the shell
        AuditLog,
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
        User,
        Warehouse,
    )

    return {"db": db, "app": app}


@app.cli.command("run-setup")
def run_setup():
    """Create the schema, default settings and demo data in one step."""
    from inventory.seed import run_seed

    run_seed(demo=True)
    print("Open http://127.0.0.1:5000 and sign in as admin / Admin@12345")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")),
            debug=app.config["DEBUG"])