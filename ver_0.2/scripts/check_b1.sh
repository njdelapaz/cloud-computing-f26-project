#!/bin/bash
# B1 gate: build the filtered trace from dataset/, find the episodes, then check filter counts
# (SPEC §9.2), sampling accuracy, validators, conversion and episodes (scripts/check_b1.py), and
# run the unit tests. Output goes to results/b1/check_b1.txt; exit code 1 if any check fails.
# Usage (from anywhere): ver_0.2/scripts/check_b1.sh [--dataset DIR]
# Environment: PYTHON, a Python with numpy, pandas and pyarrow (default: ver_0.2/.venv/bin/python)
set -euo pipefail
cd "$(dirname "$0")/.."
DATASET=dataset
while [ $# -gt 0 ]; do
  case "$1" in
    --dataset) DATASET="$2"; shift 2 ;;
    *) echo "unknown option '$1'" >&2; exit 2 ;;
  esac
done
PY="${PYTHON:-.venv/bin/python}"
mkdir -p results/b1
{
  status=0
  echo "== 0. build: filter the trace and find episodes"
  "$PY" -I workloads/convert/filter_trace.py --dataset "$DATASET"
  "$PY" -I workloads/episodes/find_episodes.py
  echo
  "$PY" -I scripts/check_b1.py || status=1
  echo
  echo "== 4. validators and unit tests"
  "$PY" -I scripts/validate.py examples || status=1
  "$PY" -I -m unittest discover -s tests 2>&1 | tail -3 || status=1
  exit "$status"
} 2>&1 | tee results/b1/check_b1.txt
