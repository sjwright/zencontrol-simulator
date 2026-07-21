#!/usr/bin/env bash
# Create .venv and install simulator + test dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv}"
ZENCONTROL_PYTHON="${ZENCONTROL_PYTHON:-../zencontrol-python}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found" >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  echo "Creating $VENV with $PYTHON"
  "$PYTHON" -m venv "$VENV"
else
  echo "Using existing $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --upgrade pip
pip install -e ".[dev]"

if [[ -d "$ZENCONTROL_PYTHON" ]]; then
  echo "Installing zencontrol-python from $ZENCONTROL_PYTHON"
  pip install -e "$ZENCONTROL_PYTHON"
else
  echo "warning: $ZENCONTROL_PYTHON not found; live protocol tests will skip"
fi

echo
echo "Ready. Activate with:  source $VENV/bin/activate"
echo "Run tests with:        pytest"
echo "Run simulator with:    zencontrol-simulator"
