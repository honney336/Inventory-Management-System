# Configuration

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | generated into `instance/secret_key` | Set it explicitly in production |
| `DATABASE_URL` | `sqlite:///inventory.db` | PostgreSQL works unchanged |
| `FLASK_CONFIG` | `default` | `development`, `production`, `testing` |
| `DEFAULT_LOCALE` | `en` | Language for a visitor who has not chosen one |
| `ITEMS_PER_PAGE` | `20` | Rows per list page |
| `SESSION_DAYS` | `7` | Session lifetime |
| `SESSION_COOKIE_SECURE` | `false`, `true` in production | Leave on in production |
| `FLASK_DEBUG` | `false` | Never enable on a reachable host |
| `MAX_UPLOAD_MB` | `5` | CSV import ceiling |
| `MAX_LINE_QUANTITY` | `1000000` | Cap on one stock line |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failures before lockout |
| `LOGIN_LOCKOUT_MINUTES` | `15` | Lockout window |
| `TRUST_PROXY_HEADERS` | `false` | Set true only behind a proxy you control |
| `ALLOW_PRIVATE_WEBHOOKS` | `false` | Re-opens SSRF, leave false |
| `APP_PORT` | `8000` | Host port published by compose |
| `WEB_CONCURRENCY` | `3` | Gunicorn workers in the container |

The compose file also reads `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` for the
database service. `.env.docker.example` lists them all.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # for SECRET_KEY
```

## Settings stored in the database

These are edited at `/settings` rather than in a file, so they survive a redeploy and can be
changed without a restart.

| Setting | What it controls |
|---|---|
| Organisation name | Shown in the header and on printed output |
| Currency symbol | Used by the money formatter |
| Service level | Safety stock in the reorder calculation |
| Review period | Window used by the stock ageing report |
| Expiry window | How far ahead the expiry watch looks |
| Webhook URL | Destination for low stock and expiry events |
| Negative stock | Whether issues may take stock below zero |
| API key | Authenticates the JSON API |

## Offline networks

Chart.js and html5-qrcode load from a CDN, so the dashboard charts and the camera scanner need
internet access. Barcodes do not: `static/js/barcode.js` draws EAN-13 and Code128 locally, so
labels and barcode printing work on a machine with no network at all.

Handheld barcode scanners behave as keyboards and type into the manual entry box on the scan
desk. That path is server side, so it works offline regardless of the camera library.

Self-host the two remaining files if you need the charts or the camera.