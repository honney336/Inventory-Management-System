# Usage

## Where things are

| Task | Where |
|---|---|
| Add or edit a product | `/products` |
| Add a new warehouse | `/warehouses` |
| Book goods in | `/stock/receive`, or a purchase order |
| Book goods out | `/sales-orders`, or `/stock/issue` |
| Stock count | product page, or `/stock/adjust` |
| Move stock between sites | `/stock/transfer` |
| Scan and check a barcode | `/scan` |
| Print shelf labels | `/products/labels` |
| Print one barcode | product page, then Print label |
| What needs reordering | `/reports/low-stock` and `/reports/reorder` |
| What is about to expire | `/reports/expiry` |
| See who changed what | `/settings/audit` |

## Stock

Stock is held per product per warehouse. Receipts, issues, adjustments, transfers and returns
all append a row to a movement ledger, so the history is never overwritten. A cycle count
records the difference as an adjustment rather than rewriting the previous figure.

Weighted average cost is recalculated on every receipt, which is what the valuation report
reads.

Stock cannot go negative unless an administrator turns that on in settings.

## Batches and expiry

A product can be flagged for batch tracking with a shelf life. Receipts then ask for a batch
number and an expiry date. Issues, sales shipments and warehouse transfers take the earliest
expiry first, so a shipment of the newer lot leaves the older lot on the shelf.

Transfers move the exact batches taken from the source warehouse, which keeps traceability
intact across sites.

The dashboard shows an expiry watch, and `/reports/expiry` lists expired and upcoming stock at
cost. Webhook events fire when stock expires or is about to.

## Orders

Purchase orders move through draft, partial receipt, full receipt and cancel. A receipt posts
real stock, so the order and the stock level cannot disagree.

Sales orders move through draft, partial fulfilment and full fulfilment. A shipment is refused
with a clear message when there is not enough stock.

One click on the reorder suggestions drafts a purchase order per supplier for everything below
its reorder point, using the supplier's lead time to set the expected date.

The reorder point itself comes from measured usage: average daily use multiplied by lead time,
plus safety stock derived from the variation in daily use at your chosen service level. The
calculation is shown on the product page.

## Reports

Valuation, low stock with restock cost, ABC analysis, stock ageing, dead stock, expiry watch,
reorder suggestions, supplier spend and the movement audit. All of them export to CSV.

## Roles

| Role | Can |
|---|---|
| admin | Everything, including users, settings and destructive routes |
| manager | Products, stock, orders and reports |
| staff | Receiving, issuing, scanning and viewing |

Role checks are enforced per route, not only in the interface. Every change is written to the
audit log with the account and the source address.

## Importing data

`/products/import` takes a CSV. Columns it understands are SKU, name, category, supplier,
unit, cost, price, reorder point, barcode, bin location, batch and expiry. Rows that would
duplicate an existing SKU or barcode are skipped and reported rather than silently dropped.

Files are capped at `MAX_UPLOAD_MB`.