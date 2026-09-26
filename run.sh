#!/usr/bin/env bash
# Run from source on macOS / Linux (first run sets up a local .venv)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q
fi
exec .venv/bin/python VintedLabel4x6.py "$@"
