#!/bin/bash
# Run one simulation inside the simulator image.
# Usage: scripts/sim_run.sh <run_name> <weights: age|size|base> <events file, relative to ver_0.2/> [plugins: stock|adaptive]
# Optional environment:
#   CONFIG        config template dir (default config/smoke)
#   ACCT          dir holding users.sim and sacctmgr.script (default: CONFIG)
#   RESULTS       results dir (default results/p1)
#   TIME_START    simulated start time, Unix seconds (default 1704067200 = 2024-01-01 UTC)
#   IMAGE         simulator image (default ver02/slurm-sim:vclock, built from docker/Dockerfile)
#   ADAPTIVE      adaptive.conf to use, relative to ver_0.2/ (plugins=adaptive only). The run's etc/
#                 gets a copy, plus copies of the PoliciesFile and ScheduleFile it names; those paths
#                 are pointed at etc/, and LogFile/PlacementLog at log/. priority/adaptive then takes
#                 its ordering weights from the policy and ignores the <weights> written to slurm.conf.
#                 Without ADAPTIVE no adaptive.conf is written, and priority/adaptive uses slurm.conf's
#                 weights exactly like stock multifactor.
#   ADAPTIVE_SET  space-separated Key=Value lines that replace or extend the adaptive.conf copy, for
#                 example "DefaultPolicy=BASE" or "FixedPolicy=U". Applied before files are copied, so
#                 "PoliciesFile=/work/<path>" picks a different policies.conf. Implies ADAPTIVE=config/adaptive.conf.
#   SIM_SET       space-separated Key=Value settings that replace or extend the run's sim.conf, for
#                 example "ClockMode=real" (the stock clock) or "SchedJobCost=0.0001" (see the virtual
#                 clock keys in docker/patches/README.md). Every run also writes CostLog=log/cost.csv.
# Output: <RESULTS>/<run_name>/ with etc/, log/ (slurmctld.log, jobcomp.log, ...) and out/ (run tool output)
set -euo pipefail

VER02="$(cd "$(dirname "$0")/.." && pwd)"
NAME="$1"; WEIGHTS="$2"; EVENTS="$3"; PLUGINS="${4:-stock}"
IMAGE="${IMAGE:-ver02/slurm-sim:vclock}"
CONFIG="${CONFIG:-config/smoke}"; ACCT="${ACCT:-$CONFIG}"; RESULTS="${RESULTS:-results/p1}"
TIME_START="${TIME_START:-1704067200}"
ADAPTIVE_SET="${ADAPTIVE_SET:-}"
SIM_SET="${SIM_SET:-}"
ADAPTIVE="${ADAPTIVE:-${ADAPTIVE_SET:+config/adaptive.conf}}"

case "$PLUGINS" in
  stock)    PT=priority/multifactor; ST=select/cons_tres ;;
  adaptive) PT=priority/adaptive;    ST=select/cons_tres_adaptive ;;
  *) echo "unknown plugins '$PLUGINS' (use stock or adaptive)" >&2; exit 2 ;;
esac
if [[ -n "$ADAPTIVE" && "$PLUGINS" != adaptive ]]; then
  echo "ADAPTIVE/ADAPTIVE_SET need plugins=adaptive" >&2; exit 2
fi

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

# sim.conf overrides (its lines are "Key = Value"); the cost log always goes to log/cost.csv
for kv in $SIM_SET "CostLog=$RUN/log/cost.csv"; do
  k="${kv%%=*}"; v="${kv#*=}"
  if grep -q "^[[:space:]]*$k[[:space:]]*=" "$RUN_HOST/etc/sim.conf"; then
    sed -i "s#^[[:space:]]*$k[[:space:]]*=.*#$k = $v#" "$RUN_HOST/etc/sim.conf"
  else
    echo "$k = $v" >> "$RUN_HOST/etc/sim.conf"
  fi
done
cp "$VER02/$ACCT/sacctmgr.script" "$VER02/$ACCT/users.sim" "$RUN_HOST/etc/"

# Replace each Key=Value of $2 in file $1 (single-key lines), appending keys not present.
set_keys() {
  awk -v sets="$2" '
    BEGIN { n = split(sets, kv, " "); for (i = 1; i <= n; i++) { k = kv[i]; sub(/=.*/, "", k); key[i] = k } }
    { for (i = 1; i <= n; i++) if ($0 ~ ("^[ \t]*" key[i] "=")) { print kv[i]; seen[i] = 1; next }; print }
    END { for (i = 1; i <= n; i++) if (!seen[i]) print kv[i] }' "$1" > "$1.tmp" && mv "$1.tmp" "$1"
}
# Value of single-key line Key in file $2 (empty if absent).
get_key() { sed -n "s/^[[:space:]]*$1=\([^[:space:]#]*\).*/\1/p" "$2" | tail -1; }
# Host path of a path written in adaptive.conf: /work/... is ver_0.2/..., relative is next to the template.
host_path() {
  case "$1" in
    /work/*) echo "$VER02/${1#/work/}" ;;
    /*) echo "adaptive.conf: $1 is outside /work, so it can't be copied into the run" >&2; return 1 ;;
    *) echo "$(dirname "$VER02/$ADAPTIVE")/$1" ;;
  esac
}

if [[ -n "$ADAPTIVE" ]]; then
  AC="$RUN_HOST/etc/adaptive.conf"
  cp "$VER02/$ADAPTIVE" "$AC"
  [[ -n "$ADAPTIVE_SET" ]] && set_keys "$AC" "$ADAPTIVE_SET"
  for k in PoliciesFile:policies.conf ScheduleFile:schedule.conf; do
    src="$(get_key "${k%%:*}" "$AC")"
    if [[ -n "$src" ]]; then cp "$(host_path "$src")" "$RUN_HOST/etc/${k#*:}"; fi
  done
  paths="PoliciesFile=$RUN/etc/policies.conf ScheduleFile=$RUN/etc/schedule.conf LogFile=$RUN/log/decisions.csv"
  [[ -n "$(get_key PlacementLog "$AC")" ]] && paths="$paths PlacementLog=$RUN/log/placements.csv"
  set_keys "$AC" "$paths"
fi

rc=0
docker run --rm -v "$VER02":/work "$IMAGE" bash -c "
  set -e
  cd $RUN
  openssl genrsa -out etc/slurm.key 2048 2>/dev/null
  openssl rsa -in etc/slurm.key -pubout -out etc/slurm.cert 2>/dev/null
  slurmsim -v run_sim -d -s /opt/slurm_sim -e $RUN/etc -a $RUN/etc/sacctmgr.script \
      -w /work/$EVENTS -r $RUN/out -dtstart 30 > $RUN/log/slurmsim.log 2>&1
" || rc=$?
if [[ -n "$ADAPTIVE" ]]; then
  # The plugin must have read the policy; a fatal config error or a silent fallback fails the run.
  if grep -q "fatal: adaptive:" "$RUN_HOST/log/slurmctld.log" 2>/dev/null; then
    echo "$NAME: priority/adaptive stopped: $(grep -m1 'fatal: adaptive:' "$RUN_HOST/log/slurmctld.log")" >&2; exit 1
  fi
  if ! grep -q "adaptive: loaded policy=" "$RUN_HOST/log/slurmctld.log" 2>/dev/null; then
    echo "$NAME: priority/adaptive did not report loading a policy (see log/slurmctld.log)" >&2; exit 1
  fi
fi
if [[ $rc != 0 ]]; then echo "$NAME: the simulator exited with status $rc (see log/slurmsim.log)" >&2; exit "$rc"; fi
echo "$NAME: done ($(grep -c '^JobId=' "$RUN_HOST/log/jobcomp.log" 2>/dev/null || echo 0) jobs completed)"
