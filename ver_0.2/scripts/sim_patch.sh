#!/bin/bash
# Edit the ver_0.2 patches to the pinned simulator source without leaving third_party/ dirty.
# The Docker build copies the pristine clone and applies docker/patches/*.patch in name order,
# so the clone itself must stay unmodified between edits.
# Usage: scripts/sim_patch.sh apply   apply docker/patches/sim_determinism.patch to the clone, to edit it
#        scripts/sim_patch.sh save    write the clone's changes back to the patch and restore the clone
#        scripts/sim_patch.sh check   fail if the clone has uncommitted changes (run before docker build)
set -euo pipefail
VER02="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$VER02/third_party/slurm_simulator"
PATCH="$VER02/docker/patches/sim_determinism.patch"
case "${1:-}" in
  apply) git -C "$SRC" apply "$PATCH" && echo "applied $(basename "$PATCH") to third_party/slurm_simulator" ;;
  save)  git -C "$SRC" diff > "$PATCH" && git -C "$SRC" checkout -- . &&
         echo "saved docker/patches/$(basename "$PATCH") ($(grep -c '^+[^+]' "$PATCH") added lines); clone restored" ;;
  check) if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
           echo "third_party/slurm_simulator has local changes; run scripts/sim_patch.sh save first" >&2; exit 1
         fi; echo "third_party/slurm_simulator is clean" ;;
  *) echo "usage: $0 apply|save|check" >&2; exit 2 ;;
esac
