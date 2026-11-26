"""The command the simulator runs at every policy tick (SPEC §7.2).

Usage (inside the container, from /work):
  python3 -m controller.tick --dir POLICY_DIR

POLICY_DIR holds:
  params.json    policy parameters (written by sim/prepare_run.py from config/triggers.yaml)
  state.json     snapshot written by the simulator for this tick (input)
  memory.json    policy memory between ticks (read and rewritten)
  decision.json  this tick's decision for the simulator (output)
  controller.jsonl  one record per tick (appended)

decision.json: {"t_sim": ..., "config": "<id>" or null, "reason": "..."}; config is set only
when the policy switches. Standard library only (runs under the container's Python 3.10).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from controller.policy import CLASSES, Memory, Params, decide


def counts_from_snapshot(state: dict) -> dict[str, int]:
    counts = {c: 0 for c in CLASSES}
    for job in state.get("waiting", []):
        q = job.get("qos")
        if q in counts:
            counts[q] += 1
    return counts


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)


def run(policy_dir: Path) -> dict:
    started = time.perf_counter()
    params = Params.from_dict(json.loads((policy_dir / "params.json").read_text()))
    state = json.loads((policy_dir / "state.json").read_text())
    mem_path = policy_dir / "memory.json"
    memory = (Memory.from_dict(json.loads(mem_path.read_text())) if mem_path.exists()
              else Memory(state=params.initial_state))
    d, memory = decide(int(state["t_sim"]), counts_from_snapshot(state), memory, params)
    out = {"t_sim": d.t_sim, "config": d.config if d.switch else None, "reason": d.reason}
    write_json(policy_dir / "decision.json", out)
    write_json(mem_path, asdict(memory))
    rec = asdict(d)
    rec["config_active_before"] = state.get("config")
    rec["command_ms"] = round((time.perf_counter() - started) * 1000, 1)
    with open(policy_dir / "controller.jsonl", "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dir", required=True, type=Path)
    a = p.parse_args()
    try:
        run(a.dir)
    except Exception as exc:          # the simulator keeps the current config on failure
        print(f"controller.tick failed: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
