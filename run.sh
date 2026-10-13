#!/usr/bin/env bash
# CivicLens local runner.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

if [ ! -d ".venv" ]; then
  echo "==> creating virtualenv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> installing dependencies"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo "==> starting CivicLens on http://${HOST}:${PORT}"
exec uvicorn civiclens.app:app --host "$HOST" --port "$PORT" "$@"