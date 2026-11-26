"""Task 1.7: check the weight configurations in the simulator via the priority debug log.

Usage:
  .venv/bin/python -m sim.check_configs [--scenario K-s1] [--hours 36] [--tag pass1]

Runs B0 and C1-C6 in parallel on the first --hours of the scenario's submissions with
DebugFlags=Priority, then per configuration:
  - checks that the weights Slurm logged equal the rendered file;
  - over snapshots of the pending queue (hourly), measures each goal's typical contribution
    gap (Gini mean difference of weight x factor across pending jobs) and checks that the
    goals rank in the configured tier order;
and over all weighted runs: the fair-share factor's typical gap among pending jobs (the scale
task 1.6 left provisional) and the correlation of U and L contributions.
Writes results/check/configs_<tag>.json and .csv (raw runs stay in results/check/<tag>-*/).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from sim.check_run import check
from sim.prepare_run import ROOT, prepare_run
from sim.priority_log import parse
from sim.weights import gini_mean_difference

OUT = ROOT / "results" / "check"
CONFIGS = ["B0", "C1", "C2", "C3", "C4", "C5", "C6"]
GOAL_OF = {"fairness": "F", "utilization": "U", "load_balance": "L"}


def file_weights(name: str) -> dict:
    w = {}
    for line in (ROOT / "config" / "priority" / f"{name}.conf").read_text().splitlines():
        if line.startswith("PriorityWeightTRES="):
            for part in line.split("=", 1)[1].split(","):
                k, v = part.split("=")
                w[f"TRES_{k.lower()}"] = float(v)
        elif line.startswith("PriorityWeight"):
            k, v = line[len("PriorityWeight"):].split("=")
            w[k] = float(v)
    return w


def slice_workload(scenario: str, hours: float, dest: Path) -> tuple[Path, Path, int]:
    src = ROOT / "workloads" / "traces" / scenario
    lines = [l for l in (src / "events.txt").read_text().splitlines()
             if int(l.split()[1]) < hours * 3600]
    dest.mkdir(parents=True, exist_ok=True)
    ev = dest / f"{scenario}-{hours:g}h.events"
    ev.write_text("\n".join(lines) + "\n")
    return ev, src / "accounts.txt", len(lines)


def run_one(name: str, tag: str, events: Path, accounts: Path, time_start: int) -> Path:
    run = Path("results/check") / f"{tag}-{name}"
    prepare_run(run, events, accounts, ROOT / "config" / "priority" / f"{name}.conf", time_start,
                extra_conf=["DebugFlags=Priority"])
    subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run), "60"], check=True)
    return run


def contributions(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"snapshot": df["snapshot"], "job_id": df["job_id"],
                         "F": df["Fairshare_v"], "U": df["TRES_cpu_v"] + df["TRES_mem_v"],
                         "L": df["JobSize_v"], "fs_factor": df["Fairshare_f"]})


def analyse(name: str, run: Path, order: list[str] | None, min_pending: int) -> dict:
    problems = check(run)
    df = parse(ROOT / run / "out" / "slurmctld.log", every_s=3600)
    res = {"config": name, "run_problems": problems, "priority_rows": int(len(df))}
    want = file_weights(name)
    got = {"Age": df["Age_w"], "Fairshare": df["Fairshare_w"], "JobSize": df["JobSize_w"],
           "TRES_cpu": df["TRES_cpu_w"], "TRES_mem": df["TRES_mem_w"]}
    if name == "B0":
        res["weights_match"] = bool(all((s == 0).all() for s in got.values()))
        return res
    # TRES weights only appear on lines whose factor is non-zero; compare where present.
    mismatch = {k: sorted(set(v[v != 0].round(2))) for k, v in got.items()
                if (v[v != 0].round(2) != round(want[k], 2)).any()}
    res["weights_match"] = not mismatch
    res["weight_mismatches"] = mismatch
    c = contributions(df)
    snaps = c.groupby("snapshot").filter(lambda g: len(g) >= min_pending)
    gaps = snaps.groupby("snapshot")[["F", "U", "L"]].agg(gini_mean_difference)
    res["snapshots_used"] = int(len(gaps))
    res["pending_per_snapshot_median"] = float(snaps.groupby("snapshot").size().median()) if len(gaps) else 0
    res["typical_gap"] = {k: round(float(gaps[k].median()), 1) for k in "FUL"} if len(gaps) else {}
    expected = [GOAL_OF[g] for g in order]
    observed = sorted("FUL", key=lambda k: -res["typical_gap"].get(k, 0))
    res["expected_order"], res["observed_order"] = expected, observed
    res["order_ok"] = expected == observed
    res["_contrib"] = snaps
    return res


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", default="K-s1")
    p.add_argument("--hours", type=float, default=36)
    p.add_argument("--tag", default="pass1")
    p.add_argument("--time-start", type=int, default=1_706_486_400)   # K week start
    p.add_argument("--min-pending", type=int, default=20)
    a = p.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "weights.yaml").read_text())
    events, accounts, n = slice_workload(a.scenario, a.hours, OUT / f"{a.tag}-input")
    print(f"{a.scenario}: first {a.hours:g} h, {n} jobs")
    with ThreadPoolExecutor(max_workers=len(CONFIGS)) as ex:
        runs = dict(zip(CONFIGS, ex.map(lambda c: run_one(c, a.tag, events, accounts,
                                                          a.time_start), CONFIGS)))

    results = [analyse(c, runs[c], cfg["configs"].get(c), a.min_pending) for c in CONFIGS]
    pooled = pd.concat([r.pop("_contrib") for r in results if "_contrib" in r])
    fs_gap = pooled.groupby("snapshot")["fs_factor"].agg(gini_mean_difference)
    summary = {
        "scenario": a.scenario, "hours": a.hours, "jobs": n,
        "fairshare_factor_gap_median": float(fs_gap.median()),
        "fairshare_factor_gap_quartiles": [float(x) for x in fs_gap.quantile([.25, .75])],
        "corr_U_L_pending": float(np.corrcoef(pooled["U"], pooled["L"])[0, 1]),
        "configs": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"configs_{a.tag}.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    rows = [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            for r in results]
    pd.DataFrame(rows).to_csv(OUT / f"configs_{a.tag}.csv", index=False)
    print(json.dumps({k: v for k, v in summary.items() if k != "configs"}, indent=2))
    for r in results:
        print(r["config"], "weights_match", r["weights_match"], "order_ok", r.get("order_ok"),
              "gap", r.get("typical_gap"), "snapshots", r.get("snapshots_used"),
              "problems", r["run_problems"])


if __name__ == "__main__":
    main()
