#!/usr/bin/env bash
# Create .venv and install simulator + test dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-}"
VENV="${VENV:-.venv}"
ZENCONTROL_PYTHON="${ZENCONTROL_PYTHON:-../zencontrol-python}"

if [[ -z "$PYTHON" ]]; then
  for candidate in python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 14) else 1)
PY
      then
        PYTHON="$(command -v "$candidate")"
        break
      fi
    fi
  done
fi

if [[ -z "$PYTHON" ]] || ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: Python 3.14+ not found (set PYTHON=... or install python@3.14)" >&2
  exit 1
fi

if ! "$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 14) else 1)
PY
then
  echo "error: $PYTHON is older than 3.14" >&2
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
echo "Dump live config with: zencontrol-dump -ip <controller-ip>"
