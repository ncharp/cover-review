#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Le module venv de Python est absent. Installe-le avec :"
  echo "  sudo apt install python3-venv"
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Installation terminée. Lance ensuite :"
echo "  ./run.sh"
