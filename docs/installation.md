# Installation

Needs Python 3.11 or newer.

```bash
cd inventory_system

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # optional locally, a secret key is generated if absent
flask --app app run-setup     # creates the database, settings and four sample products
flask --app app run           # http://127.0.0.1:5000
```

`./run.sh` does all of the above in one step.

## First sign in

Sign in as **admin / Admin@12345** and change the password before anyone else sees it.

The first ever run shows a setup page instead of a sign-in form. It closes permanently once
the administrator exists, so there is no window where two people can both claim the install.

## Sample data

The seed is deliberately small: one warehouse, two categories, one supplier and four
products, two of them batch-tracked so you can see expiry handling working.

To start with nothing but the account:

```bash
flask --app app seed --no-demo
```

Then open `/auth/setup` and create the administrator yourself.

## Docker

If you want PostgreSQL and gunicorn rather than SQLite and the development server, see
[Deployment](deployment.md).

## Next

- [Configuration](configuration.md) for the settings worth changing before real use
- [Usage](usage.md) once you are signed in