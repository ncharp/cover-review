#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 est requis pour lancer Cover Review."
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "Le module venv de Python est absent. Installe-le avec :"
    echo "  sudo apt install python3-venv"
    exit 1
  fi
  echo "Première installation : création de l'environnement Python..."
  python3 -m venv .venv
fi

# Réinstalle l'application seulement si pyproject.toml a changé.
stamp=".venv/.pyproject-stamp"
if [[ ! -f "$stamp" ]] || ! cmp -s pyproject.toml "$stamp"; then
  echo "Installation des dépendances..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e .
  cp pyproject.toml "$stamp"
fi

exec .venv/bin/cover-review
