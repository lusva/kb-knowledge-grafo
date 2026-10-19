#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  if command -v python3.13 >/dev/null 2>&1; then
    python3.13 -m venv .venv
  elif command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  else
    python3 -m venv .venv
  fi
fi

.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 10), "Serve Python 3.10 o successivo"'
if ! cmp -s requirements.txt .venv/requirements-installed.txt; then
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/requirements-installed.txt
fi
.venv/bin/python manage.py migrate --noinput
if ! .venv/bin/python manage.py shell -c 'from django.contrib.auth import get_user_model; import sys; sys.exit(0 if get_user_model().objects.filter(is_superuser=True, is_active=True).exists() else 1)' >/dev/null; then
  echo "Primo avvio: crea il tuo amministratore per Knowledge e grafo."
  .venv/bin/python manage.py createsuperuser
fi

KB_GRAFO_PORT="$(.venv/bin/python -c 'import os; import kbsite.settings; p = int(os.getenv("KB_PORT", "8010")); assert 1024 <= p <= 65535; print(p)')"
echo "Apri http://127.0.0.1:${KB_GRAFO_PORT} — per fermare il server premi Ctrl+C."
exec .venv/bin/python manage.py runserver "127.0.0.1:${KB_GRAFO_PORT}" --noreload
