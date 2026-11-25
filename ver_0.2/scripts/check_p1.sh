#!/bin/bash
# P1 gate: run the smoke simulations and check them against their predictions.
# Usage (from anywhere): ver_0.2/scripts/check_p1.sh
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VER02"

# IMAGE: simulator image (default ver02/slurm-sim:a2, built from docker/Dockerfile if missing)
export IMAGE="${IMAGE:-ver02/slurm-sim:a2}"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/Dockerfile -t "$IMAGE" .

for r in 1 2 3; do scripts/sim_run.sh "priority_age_$r" age config/smoke/priority.events; done
scripts/sim_run.sh priority_size size config/smoke/priority.events
scripts/sim_run.sh memory age config/smoke/memory.events
scripts/sim_run.sh forks_age age config/smoke/priority.events adaptive
scripts/sim_run.sh forks_size size config/smoke/priority.events adaptive

python3 scripts/check_p1.py | tee results/p1/check_p1.txt
