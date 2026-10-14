# Inventory Management System

A working inventory system for small and mid-size operations. Flask, SQLAlchemy and SQLite by
default, PostgreSQL in the container, four languages, batch and expiry tracking with
first-expiry-first-out picking, and a JSON API.

**The documentation is in [docs/](docs/index.md).**

| Page | Covers |
|---|---|
| [Installation](docs/installation.md) | Running it on your own machine, first sign in, sample data |
| [Configuration](docs/configuration.md) | Environment variables and in-app settings |
| [Usage](docs/usage.md) | Receiving, issuing, batches, orders, reports |
| [Barcodes and scanning](docs/barcodes.md) | How codes are drawn, sized, printed and read back |
| [Languages](docs/languages.md) | The four locales and adding a fifth |
| [Deployment](docs/deployment.md) | Docker and bare metal, going live, updates |
| [Operations](docs/operations.md) | Backup and restore, routine tasks, troubleshooting |
| [API](docs/api.md) | JSON endpoints and authentication |
| [Development](docs/development.md) | Code layout, tests, CLI |
| [Security](docs/security.md) | The review, what was fixed, known limits |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

flask --app app run-setup     # schema, settings, four sample products
flask --app app run           # http://127.0.0.1:5000
```

Sign in as `admin` / `Admin@12345`, then change the password. `./run.sh` does all of the above
in one command.

## Tests

```bash
pytest tests -q                     # 58 tests
node tests/barcode.test.js          # 13 encoder checks
node tests/barcode_decode.test.js   # 9 decode round-trip checks
python scripts/security_probe.py    # 0 findings
python scripts/security_probe2.py   # 0 findings
```

## Report

[docs/Inventory-System-Report.docx](docs/Inventory-System-Report.docx) is a 24 page report
covering every feature, what sets the system apart, the advanced behaviour in detail, and the
full deployment process. Regenerate it with `python scripts/build_report.py`.