# Inventory management system

A working inventory system for small and mid-size operations. Flask, SQLAlchemy and SQLite
by default, PostgreSQL in the container, four languages, batch and expiry tracking with
first-expiry-first-out picking, and a JSON API.

## Documentation map

| Page | Covers |
|---|---|
| [Project report](Inventory-System-Report.docx) | 24 page report: features, what sets it apart, advanced behaviour, deployment |
| [Installation](installation.md) | Running it on your own machine, first sign in, sample data |
| [Configuration](configuration.md) | Every environment variable and every setting you can change in the app |
| [Usage](usage.md) | Day-to-day work: receiving, issuing, batches, orders, reports |
| [Barcodes and scanning](barcodes.md) | How codes are drawn, sized, printed and read back |
| [Languages](languages.md) | The four locales, the catalogues, adding a language |
| [Deployment](deployment.md) | Docker and bare-metal install, going live, updates |
| [Operations](operations.md) | Backup and restore, routine tasks, troubleshooting |
| [API](api.md) | The JSON endpoints and how to authenticate |
| [Development](development.md) | Code layout, tests, CLI commands, helper scripts |
| [Security](security.md) | The review, what was fixed, known limits |

## What it does

| Area | What you get |
|---|---|
| Products | SKU, barcode, category, supplier, unit, cost, price, reorder point, bin location |
| Stock | Quantity per product per warehouse, full movement history, weighted average cost |
| Batches | Lot numbers and expiry dates, issues draw the earliest expiry first |
| Orders | Purchase orders and sales orders with partial receipts and partial shipments |
| Reports | Valuation, low stock, ABC, stock ageing, dead stock, expiry, reorder suggestions, supplier spend, audit trail |
| Scanning | Barcode scan desk with camera scanning and quick receive or issue |
| Access | Roles for admin, manager and staff, with an audit trail of who changed what |
| Language | English, Finnish, Chinese, Japanese |

### Behaviour worth knowing about

- **FEFO batch allocation.** On a batch-tracked product, every receipt asks for a batch number
  and expiry date. Issues, sales shipments and warehouse transfers automatically take the
  earliest expiry first, so perishable stock does not sit while newer stock ships.
- **Reorder points from measured use.** The system works out daily consumption, multiplies it
  by the supplier's lead time, and adds safety stock derived from the variation in daily use
  at a service level you choose. The calculation is shown on the product page and can be
  applied in one click.
- **The ledger cannot drift.** Every quantity change goes through one service function that
  updates the stock figure and writes the ledger entry in the same database transaction.
- **Weighted average cost** is recalculated on each receipt, so stock valuation reflects what
  you actually paid.
- **Auto-ordering** drafts one purchase order per supplier for everything below its reorder
  point, using the supplier's lead time to set the expected date.
- **Locale-aware formatting.** Finnish uses `1 234,50` and `07.03.2026`; Chinese and Japanese
  use `2026-03-07`. Each user picks their own language.

### Not included

Serial-number tracking, manufacturing and bills of material, multi-currency, purchase
approvals, demand forecasting and label printers. Expiry alerts fire when someone uses the
system, not on a timer.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

flask --app app run-setup     # schema, settings, four sample products
flask --app app run           # http://127.0.0.1:5000
```

Sign in as `admin` / `Admin@12345`, then change the password. The full walkthrough is on
[Installation](installation.md).