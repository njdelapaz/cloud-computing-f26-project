#!/bin/bash
# P2 gate: every config and example file is valid, policies.conf matches the SPEC §6 mapping,
# and the validator unit tests pass. Standard-library Python only.
# Usage (from anywhere): ver_0.2/scripts/check_p2.sh
set -euo pipefail
cd "$(dirname "$0")/.."
{
  python3 -I scripts/make_policies.py --check
  python3 -I scripts/validate.py examples
  python3 -I -m unittest discover -s tests 2>&1 | tail -3
} | tee results/p2/check_p2.txt
