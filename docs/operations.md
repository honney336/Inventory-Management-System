# Operations

## Backup and restore

**PostgreSQL**

```bash
docker compose exec -T db pg_dump -U inventory inventory > backup-$(date +%F).sql
# restore
docker compose exec -T db psql -U inventory -d inventory < backup-2026-10-14.sql
```

**SQLite**

```bash
cp data/inventory.db ~/backups/inventory-$(date +%F).db
```

Back up nightly and keep at least a month. Test a restore once, because an untested backup is
a guess.

## Routine tasks

| Task | Where |
|---|---|
| Add or edit a product | `/products` |
| Add a new warehouse | `/warehouses` |
| Book goods in | `/stock/receive`, or a purchase order |
| Book goods out | `/sales-orders`, or `/stock/issue` |
| Stock count | product page, or `/stock/adjust` |
| Move stock between sites | `/stock/transfer` |
| Scan and check a barcode | `/scan` |
| What needs reordering | `/reports/low-stock` and `/reports/reorder` |
| What is about to expire | `/reports/expiry` |
| See who changed what | `/settings/audit` |

## Troubleshooting

### Sign in

| Symptom | Cause and fix |
|---|---|
| Sign-in says cookies are not secure | Running over plain HTTP in production mode. Use HTTPS, or set `SESSION_COOKIE_SECURE=false` for local trials only |
| Cannot sign in, "too many failed sign-ins" | Five wrong attempts locks the account for 15 minutes. Wait it out, or clear the row: `docker compose exec app flask --app app shell` then `db.session.execute(db.delete(LoginAttempt))` |
| Cannot sign in after the seed | The password was changed. `docker compose exec app flask --app app shell`, then `user = db.session.execute(db.select(User).where(User.username=='admin')).scalar_one(); user.set_password('New@12345'); db.session.commit()` |
| Setup page still showing | No user exists yet. Open `/auth/setup` and create the administrator |
| Toast appears beside the login card | Stale CSS. Clear the browser cache or hard reload |

### Running

| Symptom | Cause and fix |
|---|---|
| `database not ready` on start | PostgreSQL is still starting. The entrypoint waits 60 seconds; check `docker compose logs db` |
| Port already in use | Change `APP_PORT` in `.env` |
| 413 error on CSV import | File over `MAX_UPLOAD_MB`. Split it |

### Barcodes

| Symptom | Cause and fix |
|---|---|
| Scanner reads a code but the app says no match | Check the stored barcode. A scanner returns the full 13 digit EAN for a numeric code, and an alphanumeric code exactly as typed; both forms resolve. Anything else, such as a code with a unit suffix, has to match what is stored |
| Barcode label prints blank | Barcodes are drawn locally by `static/js/barcode.js` and need no network. A value that cannot be encoded, such as non-Latin characters, shows the code as text instead |
| Label scans only from close up | Printed labels need a module width of at least 0.33mm. Browser print scaling below 100% shrinks it. Print at 100% and check background graphics is on |
| Camera scanning will not start | html5-qrcode comes from a CDN. Handheld scanners act as keyboards and work regardless; self-host the library for camera use |

### Interface

| Symptom | Cause and fix |
|---|---|
| Dashboard charts are blank | Chart.js comes from a CDN. Self-host it for an offline network |

## Webhooks

The notifier fires on low stock and expiry events. The destination must be an http or https URL
with no credentials, resolving to a public address. Loopback, private, link-local and reserved
ranges are refused unless `ALLOW_PRIVATE_WEBHOOKS` is on, which re-opens SSRF and should stay
off.

DNS is resolved once per send, so a hostname that resolves to a public address at check time
and a private one at connect time is not covered. Keep the webhook on a host you control.