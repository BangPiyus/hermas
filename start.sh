#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python setup_local.py
exec uvicorn hermas.web:create_app --factory --host 127.0.0.1 --port 8765 --workers 1
