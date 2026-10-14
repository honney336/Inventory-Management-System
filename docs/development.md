# Development

## Code layout

```
inventory_system/
├── app.py                     entry point and flask shell context
├── config.py                  configuration objects, environment driven
├── Dockerfile, docker-compose.yml, docker/entrypoint.sh
├── requirements.txt
├── docs/                      this documentation
├── scripts/
│   ├── extract_strings.py     translation tooling
│   ├── ui_check.py            headless browser check of toasts and barcodes
│   └── security_probe.py, security_probe2.py
├── tests/
│   ├── test_inventory.py
│   ├── barcode.test.js        EAN-13 and Code128 encoder checks
│   └── barcode_decode.test.js renders codes and reads them back
└── inventory/
    ├── __init__.py            application factory, CLI commands, error handlers
    ├── extensions.py          db, login manager, CSRF
    ├── models.py              users, products, batches, stock, ledger, orders, audit
    ├── services.py            stock operations, ordering, report aggregates
    ├── notifier.py            webhook events for low stock and expiry
    ├── utils.py               parsing, formatting, barcode matching, pagination, CSV
    ├── decorators.py          role guards
    ├── seed.py                administrator and four sample products
    ├── blueprints/            one module per area
    ├── templates/             Jinja templates
    ├── static/                one stylesheet, app.js and the barcode renderer
    └── i18n/                  locale resolution, formatting, catalogues
```

## Where the rules live

Stock rules live in `services.py`, not in the views. Every quantity change goes through
`receive_stock`, `issue_stock`, `adjust_stock` or `transfer_stock`, which update the on-hand
figure and write the ledger row in the same transaction, so the two cannot drift. Views never
touch quantities directly.

## CLI

```bash
flask --app app run-setup            # schema, settings, sample data
flask --app app init-db              # schema and settings only
flask --app app seed [--no-demo]     # users and sample data
flask --app app check-translations   # report untranslated strings
flask --app app routes-list          # every registered route
flask --app app shell                # db.session and all models pre-imported
```

## Tests

```bash
pytest tests -q                     # 58 tests
node tests/barcode.test.js          # 13 encoder checks
node tests/barcode_decode.test.js   # 9 decode round-trip checks
```

The pytest suite covers sign-in and role restrictions, the stock maths (weighted average,
negative stock, batch allocation), FEFO picking, batch transfers, expiry classification,
reorder suggestions, purchase and sales order lifecycles, the API, translation completeness,
locale-aware formatting, webhook delivery, barcode matching and toast placement, and a
page-by-page render check of every main page in all four languages.

The Node suites need `node` on the path. `test_barcode_encoder_unit_tests_pass` in pytest runs
them automatically when Node is available and skips when it is not.

### Checking the interface

`scripts/ui_check.py` drives a headless Chromium and reports what a person would see,
including whether a toast sits above its card and whether the barcode canvas has ink on it.

```bash
python scripts/ui_check.py http://127.0.0.1:5000 --out /tmp/shots
python scripts/ui_check.py http://127.0.0.1:8000 --out /tmp/shots --admin-password 'Admin@12345'
```

It exits non-zero when it finds a layout problem. If the account is locked out it says so
rather than timing out.

## Security probes

```bash
python scripts/security_probe.py      # injection, authz, CSRF, logic, files
python scripts/security_probe2.py     # secret key, debug, sessions, headers
```

Both run against a throwaway database. See [Security](security.md).