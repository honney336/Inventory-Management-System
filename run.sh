#!/usr/bin/env bash
# Create the virtualenv, install dependencies, seed the database and start the dev server.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "Creating .env from the example — change SECRET_KEY before deploying."
  cp .env.example .env
fi

if [ ! -f inventory.db ]; then
  echo "Creating the database, settings and sample data…"
  ./.venv/bin/flask --app app run-setup
fi

echo "Starting on http://127.0.0.1:${PORT:-5000} — sign in as admin / Admin@12345"
exec ./.venv/bin/python app.py