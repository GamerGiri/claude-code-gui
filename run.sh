#!/usr/bin/env bash
# Launch Claude Code GUI, preferring the project venv when it exists.
set -e
cd "$(dirname "$0")"

PY="python3"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "Tip: run ./setup.sh first (one time) to create .venv and install dependencies."
fi

exec "$PY" app.py "$@"
