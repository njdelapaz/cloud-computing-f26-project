#!/bin/bash
# Gate for the virtual clock (docker/patches/sim_determinism.patch): identical inputs must give
# identical runs, and with BASE loaded priority/adaptive must match stock exactly, now on realistic
# days too. Every run uses ClockMode=virtual and the fixed costs in config/*/sim.conf.in.
# Checks are listed in scripts/vclock.py (gate). All runs go in parallel; takes about 30 min (the two-day samples take longest).
# Usage (from anywhere): ver_0.2/scripts/check_vclock.sh
# Optional environment:
#   IMAGE      simulator image (default ver02/slurm-sim:vclock, built from docker/Dockerfile if missing)
#   REAL_RUNS  real-clock runs of the probe day to compare against (not gating; default: every
#              results/vclock/rf_probe_* folder present, the real-clock runs of the calibration)
#   SENS       set to 1 to also run the probe day with every cost halved and doubled (not gating)
#   REPORT_ONLY  set to 1 to skip the runs and only redo the report from the existing run folders
# Needs workloads/traces/p1_probe/ (scripts/p1_speed_probe.py --start 2024-01-15 --days 1) and the
# B1 samples 2024-01-30 seed 2 and 2024-02-12 seed 3 (workloads/convert/sample.py --convert).
# Output: results/vclock/check_vclock.txt, plus one folder per run under results/vclock/.
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VER02"
export IMAGE="${IMAGE:-ver02/slurm-sim:vclock}" RESULTS=results/vclock
mkdir -p results/vclock

scripts/sim_patch.sh check
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/Dockerfile -t "$IMAGE" .
python3 -I scripts/make_contended_events.py --check

SMOKE=config/smoke/priority.events
CONTENDED=config/smoke/contended.events
PROBE=workloads/traces/p1_probe
S0130=workloads/traces/2024-01-30_1d_w1d_s2
S0212=workloads/traces/2024-02-12_1d_w1d_s3
for d in "$PROBE" "$S0130" "$S0212"; do [[ -f "$d/jobs.events" ]] || { echo "missing $d/jobs.events" >&2; exit 1; }; done
ts() { sed -n 's/^TIME_START=//p' "$1/sim.env"; }
VIRTUAL="ClockMode=virtual"

# name weights events plugins [extra sim.conf settings]; config/smoke unless CONFIG/ACCT/TIME_START are set
run() {
  local name=$1 weights=$2 events=$3 plugins=$4 extra=${5:-}
  local set=""; [[ $plugins == adaptive ]] && set="DefaultPolicy=BASE"
  ADAPTIVE_SET="$set" SIM_SET="$VIRTUAL $extra" scripts/sim_run.sh "$name" "$weights" "$events" "$plugins" \
    > "results/vclock/$name.log" 2>&1 || echo "$name: FAILED (results/vclock/$name.log)"
}
project() { # name workload_dir time_start weights plugins [extra]
  CONFIG=config/project ACCT=$2 TIME_START=$3 run "$1" "$4" "$2/jobs.events" "$5" "${6:-}"
}

if [[ "${REPORT_ONLY:-0}" != 1 ]]; then
# The adaptive runs keep JobSize-only weights in slurm.conf, so a match can only come from the policy
run sm_stock_1 base "$SMOKE" stock &
run sm_stock_2 base "$SMOKE" stock &
run sm_adaptive size "$SMOKE" adaptive &
run sm_size size "$SMOKE" stock &
run ct_stock_1 base "$CONTENDED" stock &
run ct_stock_2 base "$CONTENDED" stock &
run ct_adaptive size "$CONTENDED" adaptive &
run ct_size size "$CONTENDED" stock &
project pr_stock_1 "$PROBE" 1705276770 base stock &
project pr_stock_2 "$PROBE" 1705276770 base stock &
project pr_adaptive "$PROBE" 1705276770 size adaptive &
project s0130_stock_1 "$S0130" "$(ts "$S0130")" base stock &
project s0130_stock_2 "$S0130" "$(ts "$S0130")" base stock &
project s0212_stock_1 "$S0212" "$(ts "$S0212")" base stock &
project s0212_stock_2 "$S0212" "$(ts "$S0212")" base stock &
if [[ "${SENS:-0}" == 1 ]]; then
  scaled() { python3 -I scripts/vclock.py scale "$1" config/project/sim.conf.in; }
  project pr_costs_half "$PROBE" 1705276770 base stock "$(scaled 0.5)" &
  project pr_costs_double "$PROBE" 1705276770 base stock "$(scaled 2)" &
fi
wait
fi

if [[ -z "${REAL_RUNS:-}" ]]; then
  REAL_RUNS="$(ls -d results/vclock/rf_probe_*/ 2>/dev/null | sed 's#/$##' | tr '\n' ' ' || true)"
fi
python3 -I scripts/vclock.py gate $REAL_RUNS | tee results/vclock/check_vclock.txt
