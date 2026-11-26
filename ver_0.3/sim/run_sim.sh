#!/bin/bash
# Run one prepared simulation (see sim/prepare_run.py) in the slurmsim:v03 container.
#
#   sim/run_sim.sh RUN_DIR [DTSTART] [--detach]
#
# RUN_DIR is relative to ver_0.3. Output goes to RUN_DIR/out/ and RUN_DIR/run.log.
# With --detach the container keeps its name (slurmsim-<run name>) so other commands can be
# run inside it with `docker exec` while the simulation is going.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${1:?usage: sim/run_sim.sh RUN_DIR [DTSTART] [--detach]}"
DTSTART="${2:-60}"
DETACH="${3:-}"
IMAGE="${SLURMSIM_IMAGE:-slurmsim:v03}"

RUN_DIR="${RUN_DIR%/}"
[ -f "$ROOT/$RUN_DIR/etc/slurm.conf" ] || { echo "not prepared: $RUN_DIR" >&2; exit 1; }
NAME="slurmsim-$(echo "$RUN_DIR" | tr '/' '-' | tr -cd 'A-Za-z0-9_.-')"
C_RUN="/work/$RUN_DIR"

INNER="set -e
mkdir -p /home/slurm/simrun/log /home/slurm/simrun/var
openssl genrsa -out $C_RUN/etc/slurm.key 2048 2>/dev/null
openssl rsa -in $C_RUN/etc/slurm.key -pubout -out $C_RUN/etc/slurm.cert 2>/dev/null
slurmsim -v run_sim -d -s /opt/slurm_sim -e $C_RUN/etc -a $C_RUN/etc/sacctmgr.script \
    -w $C_RUN/events.txt -r $C_RUN/out -dtstart $DTSTART"

docker rm -f "$NAME" >/dev/null 2>&1 || true
if [ "$DETACH" = "--detach" ]; then
    docker run -d --name "$NAME" -v "$ROOT":/work "$IMAGE" bash -c "$INNER > $C_RUN/run.log 2>&1" >/dev/null
    echo "$NAME"
else
    docker run --rm --name "$NAME" -v "$ROOT":/work "$IMAGE" bash -c "$INNER" > "$ROOT/$RUN_DIR/run.log" 2>&1
fi
