#!/usr/bin/env bash
# One-time setup for macOS / Linux: create .venv and install dependencies.
# Safe to re-run. Windows users: run setup.bat instead.
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating .venv ..."
  python3 -m venv .venv || {
    echo "Could not create a virtual environment."
    echo "Install Python 3.10+ (https://www.python.org/downloads), then re-run ./setup.sh"
    exit 1
  }
fi

PY=".venv/bin/python"
echo "Using $PY"

"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -r requirements.txt

# pywebview needs a browser engine on Linux; Qt is the reliable one.
case "$(uname -s)" in
  Linux)
    "$PY" -m pip install "pywebview[qt]" || echo "note: pywebview[qt] install failed (WebKitGTK may be used instead)"
    ;;
esac

"$PY" -c "import webview, claude_agent_sdk; print('dependencies OK')"
echo
echo "Done. Launch the app with:  ./run.sh"
