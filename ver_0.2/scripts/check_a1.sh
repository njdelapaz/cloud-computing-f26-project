#!/bin/bash
# A1 gate: priority/adaptive takes its ordering weights from one policy in policies.conf, and with
# BASE loaded it matches stock priority/multifactor (WORKFLOW A1, SPEC G1). Checks are listed in
# scripts/check_a1.py. Takes about 10 min.
# Usage (from anywhere): ver_0.2/scripts/check_a1.sh
# Optional environment:
#   IMAGE  simulator image (default ver02/slurm-sim:vclock, built from docker/Dockerfile if missing)
#   PROBE  set to 1 to also run the one-day probe trace (stock twice, adaptive once, about 11 min
#          each) for the non-gating run-to-run spread report. Needs workloads/traces/p1_probe/
#          (scripts/p1_speed_probe.py --start 2024-01-15 --days 1).
# Output: results/a1/check_a1.txt, plus one folder per run under results/a1/.
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
cd "$VER02"
export IMAGE="${IMAGE:-ver02/slurm-sim:vclock}" RESULTS=results/a1
mkdir -p results/a1

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f docker/Dockerfile -t "$IMAGE" .
python3 -I scripts/make_contended_events.py --check

# Parser unit tests, built with the image's compiler (the one the plugin is built with)
docker run --rm -v "$VER02":/work "$IMAGE" python3 -I /work/plugin/tests/test_adaptive_conf.py \
  > results/a1/unit_tests.txt 2>&1 || true

SMOKE=config/smoke/priority.events
CONTENDED=config/smoke/contended.events
PROBE_DIR=workloads/traces/p1_probe
RERUNS=()
adaptive_base() { ADAPTIVE_SET="DefaultPolicy=BASE" scripts/sim_run.sh "$1" size "$2" adaptive; }

# 1. smoke trace, exact identity. The simulator's clock advances with real time while slurmctld
# works, so host load can shift results; if the pair differs, rerun it once before trusting that.
for attempt in 1 2; do
  scripts/sim_run.sh smoke_stock_base base "$SMOKE" stock
  adaptive_base smoke_adaptive_base "$SMOKE"
  if python3 -I scripts/check_a1.py same smoke_stock_base smoke_adaptive_base; then break; fi
  if [[ $attempt == 1 ]]; then echo "smoke pair differs; rerunning it once"; RERUNS+=(smoke); fi
done
scripts/sim_run.sh smoke_stock_size size "$SMOKE" stock

# 2. contended trace: three stock repeats, adaptive BASE and the JobSize-only control, back to back
for r in 1 2 3; do scripts/sim_run.sh "contended_stock_base_$r" base "$CONTENDED" stock; done
adaptive_base contended_adaptive_base "$CONTENDED"
scripts/sim_run.sh contended_stock_size size "$CONTENDED" stock

# 3. FixedPolicy=U (Age only) must override DefaultPolicy=BASE and give submission order
ADAPTIVE_SET="DefaultPolicy=BASE FixedPolicy=U" scripts/sim_run.sh smoke_adaptive_fixed_u size "$SMOKE" adaptive

# 4. a malformed policies.conf (F's weights sum to 100001) must stop slurmctld
mkdir -p results/a1/bad_input
sed 's/^\(PolicyName=F .*Age=\)10000 /\110001 /' config/policies.conf > results/a1/bad_input/policies.conf
grep -q "Age=10001" results/a1/bad_input/policies.conf
ADAPTIVE_SET="PoliciesFile=/work/results/a1/bad_input/policies.conf" \
  timeout 600 scripts/sim_run.sh smoke_adaptive_bad size "$SMOKE" adaptive || true

# Not gating: the one-day probe trace on the 16-node project cluster
if [[ "${PROBE:-0}" == 1 ]]; then
  ts="$(python3 -I -c "import json; print(json.load(open('$PROBE_DIR/meta.json'))['time_start'])")"
  for run in probe_stock_base probe_stock_base_2; do
    CONFIG=config/project ACCT="$PROBE_DIR" TIME_START="$ts" scripts/sim_run.sh "$run" base "$PROBE_DIR/jobs.events" stock
  done
  CONFIG=config/project ACCT="$PROBE_DIR" TIME_START="$ts" ADAPTIVE_SET="DefaultPolicy=BASE" \
    scripts/sim_run.sh probe_adaptive_base size "$PROBE_DIR/jobs.events" adaptive
fi

python3 -I scripts/check_a1.py report --reruns "$(IFS=,; echo "${RERUNS[*]}")" | tee results/a1/check_a1.txt
