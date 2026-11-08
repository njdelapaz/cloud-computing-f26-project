#!/usr/bin/env bash
# Regenerate the starter workloads, run each under a policy, and compute metrics.
#
# Usage: scripts/run_baseline.sh [--metrics-only] [workload ...]
#
#   workload        names of specs in sim/workloads/specs/ (default: all)
#   --metrics-only  skip generation and simulation; recompute metrics from the
#                   existing results in sim/results/<setup>_<workload>/
#
# Environment:
#   SETUP    policy to run, from sim/etc/policies/ (default: baseline)
#   DTSTART  simulator start delay, see scripts/run_sim.sh (default: 59)
#   REPLICA  replica number (default: 1)
#
# Writes:
#   results/<setup>.csv                     one row of metrics per run
#   results/<setup>/<workload>.metrics.json full metrics, incl. per user and node
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

metrics_only=0
if [[ "${1:-}" == "--metrics-only" ]]; then
    metrics_only=1
    shift
fi
setup="${SETUP:-baseline}"
dtstart="${DTSTART:-59}"
replica="${REPLICA:-1}"

if [[ $# -gt 0 ]]; then
    workloads=("$@")
else
    workloads=()
    for spec in sim/workloads/specs/*.json; do
        workloads+=("$(basename "$spec" .json)")
    done
fi

if [[ $metrics_only -eq 0 ]]; then
    specs=()
    for w in "${workloads[@]}"; do specs+=("sim/workloads/specs/$w.json"); done
    python3 scripts/generate_workload.py "${specs[@]}"
fi

mkdir -p "results/$setup"
failed=()
for w in "${workloads[@]}"; do
    run="${setup}_${w}"
    dir="sim/results/$run/dtstart_${dtstart}_${replica}"
    if [[ $metrics_only -eq 0 ]]; then
        echo "=== $w"
        if ! SIM_START_POLICY="$setup" scripts/run_sim.sh "sim/workloads/$w.events" "$run" "$dtstart" "$replica"; then
            failed+=("$w")
            continue
        fi
    fi
    if [[ ! -f "$dir/slurm_acct.out" ]]; then
        echo "No results for $w in $dir" >&2
        failed+=("$w")
        continue
    fi
    python3 scripts/metrics.py "$dir" "sim/workloads/$w.csv" --csv "results/$setup.csv" --setup "$setup"
    cp "$dir/metrics.json" "results/$setup/$w.metrics.json"
done

echo "Summary: results/$setup.csv"
if [[ ${#failed[@]} -gt 0 ]]; then
    echo "Failed: ${failed[*]}" >&2
    exit 1
fi
