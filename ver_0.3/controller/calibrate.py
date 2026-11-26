"""Task 2.2: calibrate the queue-state triggers (SPEC §6.3) from a neutral (C0) run of K.

Usage: .venv/bin/python -m controller.calibrate RUN_DIR [--scenario K-s1]

From the run's sacct output and the scenario's jobs (for each job's size QOS), counts the
waiting jobs of each class every tick_s simulated seconds over the submission week. Over
ticks with at least n_min waiting, it records each class's share and writes its 101
quantiles (q0..q100) to config/triggers.yaml, keeping the parameters already there (SPEC
§6.6 defaults). It then replays the policy over those ticks and reports how often each state
would be active and how often it would switch: results/phase2/trigger_calibration.json.

Host-side only (pandas, PyYAML).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from controller.policy import CLASSES, Memory, Params, decide

ROOT = Path(__file__).resolve().parents[1]
TRIGGERS = ROOT / "config" / "triggers.yaml"
OUT = ROOT / "results" / "phase2"

DEFAULTS = {
    "tick_s": 600, "n_min": 20, "p_enter": 80, "p_exit": 60, "persist_ticks": 2,
    "dwell_s": 3600, "initial_state": "mix",
    "state_config": {"mix": "C0", "large": "C1", "small": "C2", "medium": "C3"},
}


def waiting_counts(run_dir: Path, scenario: str, tick_s: int) -> pd.DataFrame:
    """Waiting jobs per size class at every tick over the 7-day submission window."""
    from sim.runstats import load_acct
    acct = load_acct(run_dir)
    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / scenario / "jobs.parquet")
    acct = acct.join(jobs.set_index("name")[["qos"]], on="JobName")
    t0 = int(acct["Submit"].min())
    ticks = np.arange(t0 + tick_s, t0 + 7 * 86400 + 1, tick_s)
    out = {"t": ticks}
    for q in CLASSES:
        a = acct[acct["qos"] == q]
        sub, st = np.sort(a["Submit"].to_numpy()), np.sort(a["Start"].to_numpy())
        out[q] = np.searchsorted(sub, ticks, "right") - np.searchsorted(st, ticks, "right")
    return pd.DataFrame(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_dir", type=Path)
    p.add_argument("--scenario", default="K-s1")
    a = p.parse_args()

    cfg = {**DEFAULTS, **(yaml.safe_load(TRIGGERS.read_text()) if TRIGGERS.exists() else {})}
    counts = waiting_counts(a.run_dir, a.scenario, cfg["tick_s"])
    counts["n"] = counts[list(CLASSES)].sum(axis=1)
    busy = counts[counts["n"] >= cfg["n_min"]]
    shares = busy[list(CLASSES)].div(busy["n"], axis=0)
    cfg["quantiles"] = {q: [round(float(v), 6) for v in np.quantile(shares[q], np.linspace(0, 1, 101))]
                        for q in CLASSES}
    cfg["calibration"] = {
        "run": str(a.run_dir), "scenario": a.scenario, "configuration": "C0",
        "ticks": int(len(counts)), "ticks_used": int(len(busy)),
        "waiting_median": float(busy["n"].median()),
        "share_median": {q: round(float(shares[q].median()), 4) for q in CLASSES},
        "share_p10_p90": {q: [round(float(shares[q].quantile(.1)), 4),
                              round(float(shares[q].quantile(.9)), 4)] for q in CLASSES},
    }
    header = ("# Queue-state triggers (SPEC §6). Parameters are SPEC §6.6 defaults (approved\n"
              "# 2026-11-26); quantiles are each size class's share of waiting jobs in a C0 run\n"
              "# of K, written by: .venv/bin/python -m controller.calibrate RUN_DIR\n")
    TRIGGERS.write_text(header + yaml.safe_dump(cfg, sort_keys=False, width=100))

    # Replay the policy over the calibration ticks (states it would pick on this queue).
    params = Params.from_dict(cfg)
    mem = Memory(state=params.initial_state)
    states, switches = [], 0
    for r in counts.itertuples():
        d, mem = decide(int(r.t), {q: getattr(r, q) for q in CLASSES}, mem, params)
        states.append(d.state)
        switches += d.switch
    st = pd.Series(states)
    summary = {**cfg["calibration"],
               "replay_state_share": {k: round(float(v), 3) for k, v in st.value_counts(normalize=True).items()},
               "replay_switches": int(switches),
               "replay_switches_per_day": round(switches / 7, 2)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "trigger_calibration.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
