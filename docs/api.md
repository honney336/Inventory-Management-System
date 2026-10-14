# API

Authenticate with a session cookie, or with an `X-API-Key` header set as the `api_key` setting
in the application.

## Endpoints

```
GET   /api/health
GET   /api/summary
GET   /api/products?q=&page=&per_page=
POST  /api/products
GET   /api/products/<id>
PATCH /api/products/<id>
GET   /api/lookup?code=<barcode or sku>
GET   /api/low-stock?warehouse=<id>
GET   /api/movements?product_id=&limit=
POST  /api/stock/receive     {"sku": "...", "warehouse_id": 1, "quantity": 10}
POST  /api/stock/issue       {"sku": "...", "warehouse_id": 1, "quantity": 3}
```

## Example

```bash
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"sku":"PCB-002","name":"Controller Board v3","cost":26.00,"price":62.00}' \
     http://127.0.0.1:8000/api/products
```

## Behaviour worth knowing

- Every state-changing route requires a CSRF token, including the JSON API. A request without
  one returns 400.
- Role checks apply. A staff account gets 403 on manager and admin routes.
- `GET /api/lookup` accepts the same forms of a barcode as the scan desk, so a 13 digit EAN
  finds a product stored with 12 digits. See [Barcodes and scanning](barcodes.md).
- Responses never include password hashes. Errors carry a message and a status, never a
  traceback.