"""Task 2.6: summarize an adaptive run (SPEC §7.6 item 4).

Usage: .venv/bin/python -m controller.summarize RUN_DIR

Checks that every policy tick was logged by both sides (controller.jsonl and slurmctld.log),
that every switch the controller asked for was applied in the simulator, that replay
reproduces every decision, and that the run check passes. Reports switches per simulated
day, time in each state, and reversals (a switch back to the previous state within 2 h).
Writes RUN_DIR/../<run name>_summary.json next to the run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

from controller.replay import replay

ROOT = Path(__file__).resolve().parents[1]


def summarize(run_dir: Path) -> dict:
    from sim.check_run import KNOWN_ISSUES, check
    run = ROOT / run_dir
    pol = run / "policy"
    log = [json.loads(l) for l in (pol / "controller.jsonl").read_text().splitlines()]
    ctld = (run / "out" / "slurmctld.log").read_text(errors="replace")
    sim_ticks = re.findall(r"policy: tick t=(\d+) config=(\S+) decision=(\S+) command_ms=([\d.]+)", ctld)
    applied = re.findall(r"policy: switch (\S+) -> (\S+) \(policy\)", ctld)
    failed = len(re.findall(r"policy: tick t=\d+ command failed", ctld))

    asked = [(r["state_before"], r["state"], r["config"]) for r in log if r["switch"]]
    df = pd.DataFrame(log)
    df["dt"] = df["t_sim"].diff().fillna(0)
    span_days = (df["t_sim"].iloc[-1] - df["t_sim"].iloc[0]) / 86400 if len(df) > 1 else 0
    time_in = df.groupby("state")["dt"].sum()
    switches = df[df["switch"]]
    reversals = 0
    prev = None
    for r in switches.itertuples():
        if prev is not None and r.state == prev.state_before and r.t_sim - prev.t_sim <= 7200:
            reversals += 1
        prev = r

    # The same statistics over the submission week only (ticks continue through the drain,
    # when few jobs wait and the state just holds).
    wk = df[df["t_sim"] <= df["t_sim"].iloc[0] + 7 * 86400]
    wk_time = wk.groupby("state")["dt"].sum()
    wk_sw = wk[wk["switch"]]
    wk_rev, prev = 0, None
    for r in wk_sw.itertuples():
        if prev is not None and r.state == prev.state_before and r.t_sim - prev.t_sim <= 7200:
            wk_rev += 1
        prev = r
    gaps = wk_sw["t_sim"].diff().dropna() / 60

    problems = check(run_dir)
    res = {
        "ticks_controller": len(log), "ticks_simulator": len(sim_ticks),
        "command_failures": failed,
        "switches_asked": len(asked), "switches_applied": len(applied),
        "applied_matches_asked": [c for _, _, c in asked] == [b for _, b in applied],
        "replay_mismatches": replay(pol),
        "run_problems": problems, "known_simulator_issues": list(KNOWN_ISSUES),
        "span_days": round(span_days, 2),
        "switches_per_day": round(len(switches) / span_days, 2) if span_days else None,
        "time_share_by_state": {k: round(float(v / time_in.sum()), 3) for k, v in time_in.items()},
        "reversals_within_2h": reversals,
        "command_ms_median": float(pd.Series([float(t[3]) for t in sim_ticks]).median()) if sim_ticks else None,
        "gated_ticks": int(df["reason"].str.startswith("hold: ").mul(df["reason"].str.contains("waiting <")).sum()),
        "submission_week": {
            "switches": int(len(wk_sw)),
            "switches_per_day": round(len(wk_sw) / 7, 2),
            "time_share_by_state": {k: round(float(v / wk_time.sum()), 3) for k, v in wk_time.items()},
            "reversals_within_2h": wk_rev,
            "minutes_between_switches_median": float(gaps.median()) if len(gaps) else None,
            "switches_at_minimum_dwell": int((gaps <= 70).sum()),
            "gated_ticks": int(wk["reason"].str.contains("waiting <").sum()),
        },
    }
    res["ok"] = bool(res["ticks_controller"] == res["ticks_simulator"] and failed == 0 and
                     res["applied_matches_asked"] and not res["replay_mismatches"] and
                     not problems)
    out = run.parent / f"{run.name}_summary.json"
    out.write_text(json.dumps(res, indent=2) + "\n")
    return res


def main() -> int:
    res = summarize(Path(sys.argv[1]))
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
