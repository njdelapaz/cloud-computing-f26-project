#!/bin/bash
# A1 gate: priority/adaptive takes its ordering weights from one policy in policies.conf, and with
# BASE loaded it matches stock priority/multifactor exactly (WORKFLOW A1, SPEC G1).
# Usage (from anywhere): ver_0.2/scripts/check_a1.sh
# Optional environment:
#   IMAGE       simulator image (default ver02/slurm-sim:a1, built from docker/Dockerfile if missing)
#   SKIP_PROBE  set to 1 to skip the one-day probe pair (two runs of about 11 min each)
# Needs workloads/traces/p1_probe/ (scripts/p1_speed_probe.py --start 2024-01-15 --days 1).
# Output: results/a1/check_a1.txt, plus one folder per run under results/a1/.
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VER02"
export IMAGE="${IMAGE:-ver02/slurm-sim:a1}" RESULTS=results/a1
mkdir -p results/a1

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/Dockerfile -t "$IMAGE" .

# Parser unit tests, built with the image's compiler (the one the plugin is built with)
docker run --rm -v "$VER02":/work "$IMAGE" python3 -I /work/plugin/tests/test_adaptive_conf.py \
  > results/a1/unit_tests.txt 2>&1 || true

SMOKE=config/smoke/priority.events
PROBE=workloads/traces/p1_probe
RERUNS=()

# Run a stock/adaptive pair back to back. The simulator's clock advances with real backfill
# time, so host load can shift results; if the pair differs, rerun it once before trusting that.
#   pair <label> <stock run> <adaptive run> <events> [env assignments for both runs...]
pair() {
  local label="$1" stock="$2" adapt="$3" events="$4"; shift 4
  for attempt in 1 2; do
    env "$@" scripts/sim_run.sh "$stock" base "$events" stock
    env "$@" ADAPTIVE_SET="DefaultPolicy=BASE" scripts/sim_run.sh "$adapt" size "$events" adaptive
    if python3 -I scripts/check_a1.py same "$stock" "$adapt"; then return 0; fi
    if [[ $attempt == 1 ]]; then
      echo "$label pair differs; rerunning it once in case host load caused it"
      RERUNS+=("$label")
    fi
  done
}

# 1. smoke trace, plus a stock run under the adaptive run's slurm.conf weights (JobSize only)
pair smoke smoke_stock_base smoke_adaptive_base "$SMOKE"
scripts/sim_run.sh smoke_stock_size size "$SMOKE" stock

# 3. FixedPolicy=U (Age only) must override DefaultPolicy=BASE and give submission order
ADAPTIVE_SET="DefaultPolicy=BASE FixedPolicy=U" scripts/sim_run.sh smoke_adaptive_fixed_u size "$SMOKE" adaptive

# 4. a malformed policies.conf (F's weights sum to 100001) must stop slurmctld
mkdir -p results/a1/bad_input
sed 's/^\(PolicyName=F .*Age=\)10000 /\110001 /' config/policies.conf > results/a1/bad_input/policies.conf
grep -q "Age=10001" results/a1/bad_input/policies.conf
ADAPTIVE_SET="PoliciesFile=/work/results/a1/bad_input/policies.conf" \
  timeout 600 scripts/sim_run.sh smoke_adaptive_bad size "$SMOKE" adaptive || true

# 2. one-day probe trace on the 16-node project cluster
if [[ "${SKIP_PROBE:-0}" != 1 ]]; then
  pair probe probe_stock_base probe_adaptive_base "$PROBE/jobs.events" \
    CONFIG=config/project ACCT="$PROBE" TIME_START="$(python3 -I -c "import json; print(json.load(open('$PROBE/meta.json'))['time_start'])")"
fi

python3 -I scripts/check_a1.py report --reruns "$(IFS=,; echo "${RERUNS[*]}")" | tee results/a1/check_a1.txt
