#!/bin/bash
# Run one simulation inside the simulator image.
# Usage: scripts/sim_run.sh <run_name> <weights: age|size|base> <events file, relative to ver_0.2/> [plugins: stock|adaptive]
# Optional environment:
#   CONFIG      config template dir (default config/smoke)
#   ACCT        dir holding users.sim and sacctmgr.script (default: CONFIG)
#   RESULTS     results dir (default results/p1)
#   TIME_START  simulated start time, Unix seconds (default 1704067200 = 2024-01-01 UTC)
# Output: <RESULTS>/<run_name>/ with etc/, log/ (slurmctld.log, jobcomp.log, ...) and out/ (run tool output)
set -euo pipefail

VER02="$(cd "$(dirname "$0")/.." && pwd)"
NAME="$1"; WEIGHTS="$2"; EVENTS="$3"; PLUGINS="${4:-stock}"
IMAGE="${IMAGE:-ver02/slurm-sim:p1}"
CONFIG="${CONFIG:-config/smoke}"; ACCT="${ACCT:-$CONFIG}"; RESULTS="${RESULTS:-results/p1}"
TIME_START="${TIME_START:-1704067200}"

case "$PLUGINS" in
  stock)    PT=priority/multifactor; ST=select/cons_tres ;;
  adaptive) PT=priority/adaptive;    ST=select/cons_tres_adaptive ;;
  *) echo "unknown plugins '$PLUGINS' (use stock or adaptive)" >&2; exit 2 ;;
esac

case "$WEIGHTS" in
  age)  W='PriorityWeightAge=1000000\nPriorityWeightJobSize=0\nPriorityWeightFairshare=0\nPriorityWeightPartition=0\nPriorityWeightQOS=0' ;;
  size) W='PriorityWeightAge=0\nPriorityWeightJobSize=1000000\nPriorityWeightFairshare=0\nPriorityWeightPartition=0\nPriorityWeightQOS=0' ;;
  base) W='PriorityWeightAge=20000\nPriorityWeightJobSize=10000\nPriorityWeightFairshare=70000\nPriorityWeightPartition=0\nPriorityWeightQOS=0' ;;
  *) echo "unknown weights '$WEIGHTS' (use age, size or base)" >&2; exit 2 ;;
esac

RUN_HOST="$VER02/$RESULTS/$NAME"
RUN="/work/$RESULTS/$NAME"              # same directory as seen inside the container
rm -rf "$RUN_HOST"
mkdir -p "$RUN_HOST"/etc "$RUN_HOST"/log "$RUN_HOST"/var

for f in slurm.conf slurmdbd.conf sim.conf; do
  sed -e "s#@RUNDIR@#$RUN#g" -e "s#@EVENTS@#/work/$EVENTS#g" -e "s#@PRIORITY_WEIGHTS@#$W#" \
      -e "s#@PRIORITY_TYPE@#$PT#" -e "s#@SELECT_TYPE@#$ST#" -e "s#@TIME_START@#$TIME_START#" \
      "$VER02/$CONFIG/$f.in" > "$RUN_HOST/etc/$f"
done
chmod 600 "$RUN_HOST/etc/slurmdbd.conf"     # slurmdbd refuses to start otherwise
cp "$VER02/$ACCT/sacctmgr.script" "$VER02/$ACCT/users.sim" "$RUN_HOST/etc/"

docker run --rm -v "$VER02":/work "$IMAGE" bash -c "
  set -e
  cd $RUN
  openssl genrsa -out etc/slurm.key 2048 2>/dev/null
  openssl rsa -in etc/slurm.key -pubout -out etc/slurm.cert 2>/dev/null
  slurmsim -v run_sim -d -s /opt/slurm_sim -e $RUN/etc -a $RUN/etc/sacctmgr.script \
      -w /work/$EVENTS -r $RUN/out -dtstart 30 > $RUN/log/slurmsim.log 2>&1
"
echo "$NAME: done ($(grep -c '^JobId=' "$RUN_HOST/log/jobcomp.log" 2>/dev/null || echo 0) jobs completed)"
