"""Replay a run's controller log through the policy and compare decisions (SPEC §7.3).

Usage: python3 -m controller.replay POLICY_DIR

Re-runs controller.policy.decide over the counts (and, for A4, the median waits) logged in
controller.jsonl, starting from the initial memory, and checks that every tick's state,
switch, configuration and candidate match, and for A4 the phase and its time left.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from controller.policy import Memory, Params, decide


def replay(policy_dir: Path) -> list[str]:
    params = Params.from_dict(json.loads((policy_dir / "params.json").read_text()))
    memory = Memory(state=params.initial_state)
    mismatches = []
    lines = (policy_dir / "controller.jsonl").read_text().splitlines()
    for i, line in enumerate(lines):
        rec = json.loads(line)
        # with the arrivals signal, counts are arrivals and the gate also needs the waiting total
        d, memory = decide(rec["t_sim"], rec["counts"], memory, params,
                           waiting=rec["waiting"] if params.signal == "arrivals" else None,
                           median_wait_s=rec.get("median_wait_s"))
        keys = ("state", "switch", "config", "candidate")
        if params.signal == "wait_target":
            keys += ("phase", "phase_left_s")
        for key in keys:
            if getattr(d, key) != rec[key]:
                mismatches.append(f"tick {i} (t={rec['t_sim']}): {key} replay={getattr(d, key)!r} "
                                  f"online={rec[key]!r}")
    return mismatches


def main() -> int:
    policy_dir = Path(sys.argv[1])
    n = len((policy_dir / "controller.jsonl").read_text().splitlines())
    bad = replay(policy_dir)
    if bad:
        print(f"FAIL: {len(bad)} mismatch(es) over {n} ticks")
        for b in bad[:20]:
            print(" -", b)
        return 1
    print(f"OK: replay reproduces all {n} ticks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
