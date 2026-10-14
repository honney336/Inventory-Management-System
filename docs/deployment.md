# Deployment

## Docker (recommended)

Needs Docker with the Compose plugin.

```bash
cp .env.docker.example .env
```

Edit `.env` and set two values:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # for SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # for POSTGRES_PASSWORD
```

Then:

```bash
docker compose up -d --build
docker compose exec app flask --app app seed    # first run only
```

The app is on `http://localhost:8000`. Compose runs it under gunicorn with three workers
against PostgreSQL, with a health check on `/api/health` and named volumes for the database and
app data.

| Command | What it does |
|---|---|
| `docker compose logs -f app` | follow the application log |
| `docker compose restart app` | restart without rebuilding |
| `docker compose down` | stop, keep the data |
| `docker compose down -v` | stop and delete the database |
| `docker compose exec app flask --app app check-translations` | translation coverage |
| `docker compose exec app flask --app app routes-list` | every route |

The image runs as a non-root user and declares a health check, so `docker inspect` reports
status even outside compose.

### SQLite instead of PostgreSQL

Set this in `.env` and delete the `depends_on` block from `docker-compose.yml`:

```ini
DATABASE_URL=sqlite:////app/data/inventory.db
```

SQLite lives on the `app-data` volume, so it survives restarts and rebuilds.

## Without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export FLASK_CONFIG=production
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/inventory

flask --app app init-db      # creates tables, does not load sample data
gunicorn --bind 0.0.0.0:8000 --workers 3 --threads 4 "app:create_app('production')"
```

Put nginx, Caddy or a load balancer in front for TLS. Production mode already forces `Secure`
cookies and keeps the debugger off, so the app refuses plain-HTTP sign-in on purpose.

## Going live

1. Change the administrator password, or seed with `--no-demo` and create the account through
   the setup page.
2. Set `SECRET_KEY` from a real secret generator.
3. Serve over HTTPS. Production mode forces `Secure` cookies.
4. Delete the sample products, categories and supplier once your own data is in.
5. Create the users your staff need and give each one a role.
6. Confirm the backup in [Operations](operations.md) actually restores.

## Updates

```bash
docker compose exec app flask --app app init-db     # creates anything new, keeps data
git pull
docker compose up -d --build
```

Existing tables are never dropped. Adding a column is handled automatically. Anything
structural should be tried on a copy of the database first, which the backup command gives
you.