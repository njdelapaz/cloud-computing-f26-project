"""Task 2.6: summarize an adaptive run (SPEC §7.6 item 4).

Usage: .venv/bin/python -m controller.summarize RUN_DIR

Checks that every policy tick was logged by both sides (controller.jsonl and slurmctld.log),
that every switch the controller asked for was applied in the simulator, that replay
reproduces every decision, and that the run check passes. Reports switches per simulated
day, time in each state, reversals (a switch back to the previous state within 2 h), and
how long each state lasted against its minimum stay (dwell_s, one value or one per state).
For A4 (SPEC §9.3) it also reports the spells: per state, how many and how many a day, the
time in each phase, the class's median wait at the trigger and at the return, and how many
spells started at the first armed tick after a stabilization period (the trigger was still
holding). A4 statistics cover the first --window-h hours of ticks (default 168, a week; 84
for the setup workloads D).
Writes RUN_DIR/../<run name>_summary.json next to the run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

from controller.policy import Params
from controller.replay import replay

ROOT = Path(__file__).resolve().parents[1]


def a4_spells(log: list[dict], t_end: int, persist_ticks: int) -> dict:
    """Spells of an A4 run (SPEC §9.3), over ticks up to t_end.

    A spell "started at the first chance" when it began persist_ticks - 1 ticks after the
    trigger was first read again after the previous spell: the trigger was still holding
    when the stabilization period ended.
    """
    log = [r for r in log if r["t_sim"] <= t_end]
    spells, cur, last_return = [], None, None
    for i, r in enumerate(log):
        if r["switch"] and r["state"] != "base":
            first_read = None
            if last_return is not None:
                first_read = next((k for k in range(last_return + 1, i + 1)
                                   if log[k]["phase"] == "armed"
                                   and log[k]["phase_before"] in ("armed", "stabilizing")), None)
            cur = {"state": r["state"], "t_start": r["t_sim"],
                   "wait_at_trigger_s": r["median_wait_s"].get(r["state"]),
                   "first_chance": first_read is not None and i - first_read == persist_ticks - 1}
            spells.append(cur)
        elif r["switch"] and r["state"] == "base":
            last_return = i
            if cur is not None:
                cur["t_end"] = r["t_sim"]
                cur["wait_at_return_s"] = r["median_wait_s"].get(cur["state"])
                cur = None
    if not log:
        return {"spells": [], "by_state": {}, "time_share_by_phase": {}}
    span = (log[-1]["t_sim"] - log[0]["t_sim"]) or 1
    dt = [log[k + 1]["t_sim"] - log[k]["t_sim"] for k in range(len(log) - 1)] + [0]
    phase_time = {}
    for r, s in zip(log, dt):
        key = r["phase"] if r["phase"] != "active" else f"active-{r['state']}"
        phase_time[key] = phase_time.get(key, 0) + s
    by_state = {}
    for st in sorted({s["state"] for s in spells}):
        g = [s for s in spells if s["state"] == st]
        ws = [s["wait_at_trigger_s"] for s in g if s["wait_at_trigger_s"] is not None]
        we = [s["wait_at_return_s"] for s in g if s.get("wait_at_return_s") is not None]
        med = lambda v: round(float(pd.Series(v).median()) / 60, 1) if v else None
        by_state[st] = {"spells": len(g), "per_day": round(len(g) / (span / 86400), 2),
                        "median_wait_at_trigger_min": med(ws),
                        "median_wait_at_return_min": med(we),
                        "started_at_first_chance": sum(s["first_chance"] for s in g)}
    total = sum(phase_time.values()) or 1
    return {"spells": spells, "by_state": by_state,
            "time_share_by_phase": {k: round(v / total, 3) for k, v in sorted(phase_time.items())}}


def summarize(run_dir: Path, window_h: float = 168) -> dict:
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
    t_end = df["t_sim"].iloc[0] + window_h * 3600
    wk = df[df["t_sim"] <= t_end]
    wk_time = wk.groupby("state")["dt"].sum()
    wk_sw = wk[wk["switch"]]
    wk_rev, prev = 0, None
    for r in wk_sw.itertuples():
        if prev is not None and r.state == prev.state_before and r.t_sim - prev.t_sim <= 7200:
            wk_rev += 1
        prev = r
    gaps = wk_sw["t_sim"].diff().dropna() / 60
    # Stays between switches, by the state that was left. A stay is "at the minimum" when the
    # switch came at the first tick the state's own minimum stay allowed.
    params = Params.from_dict(json.loads((pol / "params.json").read_text()))
    tick_s = int(df["t_sim"].diff().median()) if len(df) > 1 else 0
    stays = pd.DataFrame({"state": wk_sw["state"].iloc[:-1].to_numpy(),
                          "stay_s": wk_sw["t_sim"].diff().dropna().to_numpy()})
    # the minimum stay: A1-A3's dwell; for A4 a spell's length, or the stabilization period
    a4 = params.signal == "wait_target"
    least = ((lambda st: params.stable_s if st == "base" else params.active_for(st)) if a4
             else params.dwell_for)
    stays["at_min"] = [s <= least(st) + tick_s for st, s in zip(stays["state"], stays["stay_s"])]
    by_state = stays.groupby("state")

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
            "switches_per_day": round(len(wk_sw) / (window_h / 24), 2),
            "time_share_by_state": {k: round(float(v / wk_time.sum()), 3) for k, v in wk_time.items()},
            "reversals_within_2h": wk_rev,
            "minutes_between_switches_median": float(gaps.median()) if len(gaps) else None,
            "switches_at_minimum_dwell": int(stays["at_min"].sum()),
            "stays_by_state": {st: {"stays": int(len(g)),
                                    "minimum_stay_min": least(st) / 60,
                                    "median_stay_min": float(g["stay_s"].median() / 60),
                                    "at_minimum": int(g["at_min"].sum())}
                               for st, g in by_state},
            "gated_ticks": int(wk["reason"].str.contains("waiting <").sum()),
        },
    }
    res["window_h"] = window_h
    if a4:
        res["a4"] = a4_spells(log, t_end, params.persist_ticks)
    res["ok"] = bool(res["ticks_controller"] == res["ticks_simulator"] and failed == 0 and
                     res["applied_matches_asked"] and not res["replay_mismatches"] and
                     not problems)
    out = run.parent / f"{run.name}_summary.json"
    out.write_text(json.dumps(res, indent=2) + "\n")
    return res


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_dir", type=Path)
    p.add_argument("--window-h", type=float, default=168)
    a = p.parse_args()
    res = summarize(a.run_dir, a.window_h)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
