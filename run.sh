#!/usr/bin/env bash
# Start MediRemind. Creates the virtualenv on first run.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

if [ ! -d .venv ]; then
  echo "Setting up the virtual environment (first run only)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Note: 'tesseract' is not installed, so reading prescriptions from a photo is off."
  echo "      Install it with:  sudo apt-get install tesseract-ocr"
  echo "      Typing the prescription in still works and sets the same alarms."
fi

echo "MediRemind is running at http://localhost:${PORT}"
exec ./.venv/bin/uvicorn backend.main:app --host "$HOST" --port "$PORT" "$@"
