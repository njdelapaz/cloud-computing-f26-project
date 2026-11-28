"""The command the simulator runs at every policy tick (SPEC §7.2).

Usage (inside the container, from /work):
  python3 -m controller.tick --dir POLICY_DIR

POLICY_DIR holds:
  params.json    policy parameters (written by sim/prepare_run.py from config/triggers.yaml)
  state.json     snapshot written by the simulator for this tick (input)
  memory.json    policy memory between ticks (read and rewritten)
  arrivals_history.json  running submission totals of recent ticks (arrivals signal only)
With the wait_target signal (A4), the median waited_s of each class's waiting jobs is taken
from the snapshot as well.
  decision.json  this tick's decision for the simulator (output)
  controller.jsonl  one record per tick (appended)

decision.json: {"t_sim": ..., "config": "<id>" or null, "reason": "..."}; config is set only
when the policy switches. Standard library only (runs under the container's Python 3.10).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
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


def median_waits_from_snapshot(state: dict) -> dict[str, float | None]:
    """Per size class, the median time its waiting jobs have waited (waited_s, SPEC §9.3);
    None for a class with no job waiting."""
    waited = {c: [] for c in CLASSES}
    for job in state.get("waiting", []):
        q = job.get("qos")
        if q in waited:
            waited[q].append(float(job.get("waited_s", 0)))
    return {c: (statistics.median(v) if v else None) for c, v in waited.items()}


def arrivals_from_totals(history: list, t: int, submitted: dict, window_s: int
                         ) -> tuple[dict[str, int], list]:
    """Jobs per size class submitted in (t - window_s, t], from running totals.

    history: earlier [t, totals] pairs (oldest first). The baseline is the latest entry at
    or before t - window_s; before the first full window, the totals since the start. Returns
    the counts and the history to keep (this tick added, entries no longer needed dropped).
    """
    now = {c: int(submitted.get(c, 0)) for c in CLASSES}
    base = {c: 0 for c in CLASSES}
    for ht, tot in history:
        if ht <= t - window_s:
            base = {c: int(tot.get(c, 0)) for c in CLASSES}
    counts = {c: now[c] - base[c] for c in CLASSES}
    keep = [h for h in history if h[0] > t - window_s]
    older = [h for h in history if h[0] <= t - window_s]
    if older:                       # the baseline for the next ticks
        keep.insert(0, older[-1])
    keep.append([t, now])
    return counts, keep


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
    t = int(state["t_sim"])
    waiting = counts_from_snapshot(state)
    extra = {}
    if params.signal == "arrivals":
        hist_path = policy_dir / "arrivals_history.json"
        history = json.loads(hist_path.read_text()) if hist_path.exists() else []
        if "submitted" not in state:
            raise ValueError("state.json has no 'submitted' totals (simulator image without "
                             "the arrivals counter)")
        arrivals, history = arrivals_from_totals(history, t, state["submitted"], params.window_s)
        d, memory = decide(t, arrivals, memory, params, waiting=sum(waiting.values()))
        write_json(hist_path, history)
        extra = {"waiting_counts": waiting, "submitted": state["submitted"]}
    elif params.signal == "wait_target":
        d, memory = decide(t, waiting, memory, params,
                           median_wait_s=median_waits_from_snapshot(state))
    else:
        d, memory = decide(t, waiting, memory, params)
    out = {"t_sim": d.t_sim, "config": d.config if d.switch else None, "reason": d.reason}
    write_json(policy_dir / "decision.json", out)
    write_json(mem_path, asdict(memory))
    rec = asdict(d)
    rec["signal"] = params.signal
    rec.update(extra)
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
