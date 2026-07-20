#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Cover Review n'est pas encore installé. Lance d'abord :"
  echo "  ./install.sh"
  exit 1
fi

source .venv/bin/activate
exec python app.py
