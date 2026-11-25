#!/bin/bash
# B2 gate: metrics on the hand-computed trace and the examples/ logs match the hand calculation,
# absent logs give empty values (scripts/check_b2.py), and the unit tests pass. Real runs are
# reported but don't gate. Output goes to results/b2/check_b2.txt; exit code 1 if anything fails.
# Usage (from anywhere): ver_0.2/scripts/check_b2.sh [--data DIR]
#   DIR: a ver_0.2 checkout holding the git-ignored real runs and traces (default: this one)
# Environment: PYTHON (default: ver_0.2/.venv/bin/python if present, else python3). The gate itself
# is standard-library only; numpy, when present, adds the per-second cross-checks on real runs.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA=.
while [ $# -gt 0 ]; do
  case "$1" in
    --data) DATA="$2"; shift 2 ;;
    *) echo "unknown option '$1'" >&2; exit 2 ;;
  esac
done
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
mkdir -p results/b2
{
  status=0
  "$PY" -I scripts/check_b2.py --data "$DATA" || status=1
  echo
  echo "== unit tests"
  "$PY" -I -m unittest discover -s tests 2>&1 | tail -3 || status=1
  exit "$status"
} 2>&1 | tee results/b2/check_b2.txt
