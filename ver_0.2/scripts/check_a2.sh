#!/bin/bash
# A2 gate: switching in priority/adaptive (WORKFLOW A2, SPEC G2, and G1 kept). Checks are listed in
# scripts/check_a2.py. Every run uses the virtual clock. All runs go in parallel; takes about
# 30 min (the two-day sample in the swap-cost measurement takes longest).
# Usage (from anywhere): ver_0.2/scripts/check_a2.sh
# Optional environment:
#   IMAGE        simulator image (default ver02/slurm-sim:a2, built from docker/Dockerfile if missing)
#   COST         set to 0 to skip the swap-cost runs (forced F<->U alternation every tick on the
#                probe day and on the 2024-02-12 seed 3 sample; not gating, used for the
#                sim_charge decision)
#   REPORT_ONLY  set to 1 to skip the tests and runs and only redo the report from what's there
# Needs workloads/traces/p1_probe/ (scripts/p1_speed_probe.py --start 2024-01-15 --days 1) and,
# with COST, the B1 sample 2024-02-12 seed 3 (workloads/convert/sample.py --convert).
# Output: results/a2/check_a2.txt, results/a2/unit_tests.txt, plus one folder per run under results/a2/.
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VER02"
export IMAGE="${IMAGE:-ver02/slurm-sim:a2}" RESULTS=results/a2
mkdir -p results/a2

SMOKE=config/smoke/priority.events
CONTENDED=config/smoke/contended.events
PROBE=workloads/traces/p1_probe
S0212=workloads/traces/2024-02-12_1d_w1d_s3
G2_SET="DefaultPolicy=BASE TMin=10min ScheduleFile=/work/config/smoke/schedule_g2.conf"

if [[ "${REPORT_ONLY:-0}" != 1 ]]; then
scripts/sim_patch.sh check
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/Dockerfile -t "$IMAGE" .
python3 -I scripts/make_contended_events.py --check
[[ -f "$PROBE/jobs.events" ]] || { echo "missing $PROBE/jobs.events" >&2; exit 1; }
[[ "${COST:-1}" == 0 || -f "$S0212/jobs.events" ]] || { echo "missing $S0212/jobs.events (or set COST=0)" >&2; exit 1; }

# Unit tests: the C ones built with the image's compiler (the one the plugin is built with), and
# the Python validator tests
{
  for t in test_decide test_adaptive_conf test_adaptive_policy; do
    echo "== plugin/tests/$t.py"
    docker run --rm -v "$VER02":/work "$IMAGE" python3 -I "/work/plugin/tests/$t.py" 2>&1 || true
  done
  echo "== tests/ (scripts/validate.py and workloads)"
  python3 -I -m unittest discover -s tests 2>&1 || true
} > results/a2/unit_tests.txt

# name weights events plugins [ADAPTIVE_SET]; config/smoke unless CONFIG/ACCT/TIME_START are set
run() {
  local name=$1 weights=$2 events=$3 plugins=$4 set=${5:-}
  ADAPTIVE_SET="$set" scripts/sim_run.sh "$name" "$weights" "$events" "$plugins" \
    > "results/a2/$name.log" 2>&1 || echo "$name: FAILED (results/a2/$name.log)"
}
project() { # name workload_dir time_start weights plugins [ADAPTIVE_SET]
  CONFIG=config/project ACCT=$2 TIME_START=$3 run "$1" "$4" "$2/jobs.events" "$5" "${6:-}"
}
ts() { sed -n 's/^TIME_START=//p' "$1/sim.env"; }

# G1: stock vs adaptive BASE with no windows (the adaptive runs keep JobSize-only weights in
# slurm.conf, so a match can only come from the policy)
run sm_stock base "$SMOKE" stock &
run sm_adaptive size "$SMOKE" adaptive "DefaultPolicy=BASE" &
run ct_stock base "$CONTENDED" stock &
run ct_adaptive size "$CONTENDED" adaptive "DefaultPolicy=BASE" &
project pr_stock "$PROBE" 1705276770 base stock &
project pr_adaptive "$PROBE" 1705276770 size adaptive "DefaultPolicy=BASE" &
# G2: the scripted windows, and the static U run that the one-off window's starts must match
run g2_window size "$CONTENDED" adaptive "$G2_SET" &
run ct_fixed_u size "$CONTENDED" adaptive "DefaultPolicy=BASE FixedPolicy=U" &
# FORCED in the plugin: F and U every tick
run ct_forced size "$CONTENDED" adaptive "DefaultPolicy=BASE ForceAlternate=F,U ForcePeriod=1min" &
if [[ "${COST:-1}" != 0 ]]; then
  project pr_forced "$PROBE" 1705276770 size adaptive "DefaultPolicy=BASE ForceAlternate=F,U ForcePeriod=1min" &
  project s0212_adaptive "$S0212" "$(ts "$S0212")" size adaptive "DefaultPolicy=BASE" &
  project s0212_forced "$S0212" "$(ts "$S0212")" size adaptive "DefaultPolicy=BASE ForceAlternate=F,U ForcePeriod=1min" &
fi
wait
fi

python3 -I scripts/check_a2.py | tee results/a2/check_a2.txt
