#!/usr/bin/env bash
# Run one simulation of a workload on the simulated 24-GPU cluster.
#
# Usage: scripts/run_sim.sh <events-file> [run-name] [dtstart] [replica]
#
#   events-file  workload in the simulator's events format, relative to the
#                repository root (e.g. sim/workloads/test20.events)
#   run-name     label for this run (default: events file name)
#   dtstart      seconds between controller start and first job (default: 59);
#                the simulator's main source of run-to-run variation
#   replica      replica number for repeated runs (default: 1)
#
# Environment:
#   SIM_START_POLICY      policy active at the start (default: baseline);
#                         one of sim/etc/policies/<name>.conf
#   SIM_MAX_REAL_SECONDS  wall-clock limit for the run (default: 1800)
#   SLURM_SIM_IMAGE       simulator image (default: gpu24-slurm-sim:latest,
#                         built from docker/Dockerfile if missing)
#
# Results land in sim/results/<run-name>/dtstart_<dtstart>_<replica>/:
#   slurm_acct.out        sacct output for every job (the main result)
#   policy_switches.csv   simulated time, old and new policy for each switch
#   slurmctld.log         full controller log
set -euo pipefail

if [[ $# -lt 1 ]]; then
    sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${SLURM_SIM_IMAGE:-gpu24-slurm-sim:latest}"
CONTAINER_REPO=/home/slurm/project

events="$1"
run_name="${2:-$(basename "$events" .events)}"
dtstart="${3:-59}"
replica="${4:-1}"
start_policy="${SIM_START_POLICY:-baseline}"
# Wall-clock limit for the whole run. The simulator keeps spinning if jobs
# never finish (e.g. all rejected at submit), so every run is capped.
max_real_seconds="${SIM_MAX_REAL_SECONDS:-1800}"

if [[ ! -f "$REPO/$events" ]]; then
    echo "Events file not found: $REPO/$events" >&2
    exit 1
fi
if [[ ! -f "$REPO/sim/etc/policies/${start_policy}.conf" ]]; then
    echo "Policy not found: sim/etc/policies/${start_policy}.conf" >&2
    exit 1
fi
# Every policy named in a switch_policy event must exist, or the switch
# would fail partway through the run.
for p in $(sed -n -E 's/.*-e +switch_policy *\| *([A-Za-z0-9_-]+).*/\1/p' "$REPO/$events" | sort -u); do
    if [[ ! -f "$REPO/sim/etc/policies/${p}.conf" ]]; then
        echo "Events file switches to unknown policy '$p' (no sim/etc/policies/${p}.conf)" >&2
        exit 1
    fi
done

if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    if [[ "$IMAGE" == "gpu24-slurm-sim:latest" ]]; then
        echo "Building simulator image $IMAGE (one time)..."
        docker build -q -f "$REPO/docker/Dockerfile" -t "$IMAGE" "$REPO" > /dev/null
    else
        echo "Docker image not found: $IMAGE" >&2
        exit 1
    fi
fi

results="sim/results/${run_name}/dtstart_${dtstart}_${replica}"
mkdir -p "$REPO/sim/log" "$REPO/sim/var" "$REPO/$results"

# slurmdbd refuses to start unless its config is private to its owner.
# Git does not keep this mode, so set it on every run.
chmod 600 "$REPO/sim/etc/slurmdbd.conf"

# slurm.conf includes policy_active.conf; start from the chosen policy.
cp "$REPO/sim/etc/policies/${start_policy}.conf" "$REPO/sim/etc/policy_active.conf"

echo "Running ${events} as '${run_name}' (dtstart=${dtstart}, replica=${replica}, start policy=${start_policy})"

# The container's user 'slurm' has uid 1000, so files written to the mounted
# repository are owned by the host user only when that user is also uid 1000.
if [[ "$(id -u)" != "1000" ]]; then
    echo "Warning: host uid is $(id -u), not 1000; result files may be owned by another user." >&2
fi

# MariaDB has to be started as root; the simulation itself runs as 'slurm'.
# Run detached so the wall-clock limit can stop the container if needed.
container="slurmsim_${run_name}_${dtstart}_${replica}_$$"
docker run -d --name "$container" \
    -v "$REPO":"$CONTAINER_REPO" \
    --entrypoint /bin/bash \
    "$IMAGE" -c "
        set -e
        /usr/local/sbin/cmd_start mysqld > /dev/null 2>&1
        cd $CONTAINER_REPO
        su slurm -c 'PATH=/opt/conda/bin:/opt/slurm_sim_tools/bin:\$PATH slurmsim -v run_sim -d \
            -e $CONTAINER_REPO/sim/etc \
            -a $CONTAINER_REPO/sim/etc/sacctmgr.script \
            -w $CONTAINER_REPO/$events \
            -r $CONTAINER_REPO/$results \
            -dtstart $dtstart'
    " > /dev/null
trap 'docker rm -f "$container" > /dev/null 2>&1 || true' EXIT

status=0
exit_code="$(timeout "$max_real_seconds" docker wait "$container")" || status=$?
docker logs "$container" > "$REPO/sim/log/run_sim.out" 2>&1 || true
if [[ $status -ne 0 ]]; then
    echo "Simulation exceeded ${max_real_seconds}s of real time and was stopped." >&2
    echo "Jobs may never finish (e.g. rejected at submit); see sim/log/slurmctld.log" >&2
    exit 1
fi
if [[ "$exit_code" != "0" ]]; then
    echo "Simulation failed (exit $exit_code); see sim/log/run_sim.out" >&2
    tail -20 "$REPO/sim/log/run_sim.out" >&2
    exit 1
fi
# slurmsim clears the results directory while it runs, so copy the
# console output in afterwards.
cp "$REPO/sim/log/run_sim.out" "$REPO/$results/run_sim.out"

# Switch log: one row per policy switch, from the controller log.
{
    echo "sim_time,from_policy,to_policy,status"
    echo "start,,${start_policy},ok"
    sed -n -E 's/^\[([^]]+)\] SIM_SWITCH_POLICY: begin (.+) -> (.+)$/\1,\2,\3/p' \
        "$REPO/$results/slurmctld.log" | sed 's/,(start),/,'"${start_policy}"',/' | while IFS= read -r row; do
            echo "${row},ok"
        done
    sed -n -E 's/^\[([^]]+)\] error: SIM_SWITCH_POLICY: (.*)$/\1,,,error: \2/p' "$REPO/$results/slurmctld.log"
} > "$REPO/$results/policy_switches.csv"
if grep -q 'error: SIM_SWITCH_POLICY' "$REPO/$results/slurmctld.log"; then
    echo "Warning: a policy switch failed; see $results/policy_switches.csv" >&2
fi

sed 's/\x1b\[[0-9;]*m//g' "$REPO/$results/run_sim.out" | grep -E 'INFO\] (Simulated time|Real time|Acceleration)' || true

# slurmctld can die mid-run while the wrapper still exits 0.
if grep -q '\] fatal: ' "$REPO/$results/slurmctld.log"; then
    echo "slurmctld hit a fatal error:" >&2
    grep '\] fatal: ' "$REPO/$results/slurmctld.log" | head -3 >&2
    exit 1
fi
n_jobs=0
if [[ -s "$REPO/$results/slurm_acct.out" ]]; then
    n_jobs=$(($(wc -l < "$REPO/$results/slurm_acct.out") - 1))
fi
if [[ $n_jobs -le 0 ]]; then
    echo "No job records produced; see $results/run_sim.out and sim/log/" >&2
    exit 1
fi
echo "Jobs recorded: $n_jobs"
echo "Policy switches: $(($(wc -l < "$REPO/$results/policy_switches.csv") - 2))"
echo "Results: $results"
