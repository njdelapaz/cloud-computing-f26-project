"""Check the weight configurations in the simulator via the priority debug log.

Usage:
  .venv/bin/python -m sim.check_configs [--scenario K-s1] [--hours 36] [--tag pass1]

Runs B0 and every configuration in config/weights.yaml in parallel on the first --hours of
the scenario's submissions with DebugFlags=Priority. Per configuration, over hourly snapshots
of the waiting queue:
  - the weights Slurm logged equal the rendered file, and each job's QOS factor equals its
    size class's configured value;
  - favored class first: share of cross-class pairs of waiting jobs where the job of the more
    favored class has the higher priority (pass: all of them with strict size, else >= 0.9);
  - then shortest time limit, then fairness: within one class, the typical time-limit gap
    exceeds the typical fair-share gap;
  - cluster state over the window: node imbalance (sigma of per-node utilization),
    utilization and mean wait (load balance is measured on the cluster, not the queue).
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
CONFIGS = ["B0"] + list(yaml.safe_load((ROOT / "config" / "weights.yaml").read_text())["configs"])


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


def class_dominance(snap: pd.DataFrame, factor_of: dict[str, float]) -> tuple[int, int]:
    """Cross-class pairs of waiting jobs, and how many the more favored class's job leads.

    For every pair of classes a, b whose QOS factors differ, counts pairs (i in a, j in b)
    and those where the job of the more favored class has the higher total priority.
    """
    pairs = wins = 0
    by = {q: np.sort(g["priority"].to_numpy()) for q, g in snap.groupby("qos")}
    for a, pa in by.items():
        for b, pb in by.items():
            if factor_of[a] > factor_of[b]:
                # for each favored job, number of other-class jobs strictly below it
                wins += int(np.searchsorted(pb, pa, side="left").sum())
                pairs += len(pa) * len(pb)
    return pairs, wins


def analyse(name: str, run: Path, config: dict | None, jobs: pd.DataFrame, min_pending: int,
            hours: float) -> dict:
    from sim.runstats import cluster_balance, load_acct
    from sim.weights import qos_factors

    problems = check(run)
    acct = load_acct(run)
    bal = cluster_balance(acct, until_s=hours * 3600)
    res = {"config": name, "run_problems": problems,
           "cluster": {"node_imbalance_sigma": round(bal["sigma_u"], 4),
                       "utilization": round(bal["util"], 4),
                       "mean_wait_min": round(float(acct["wait_s"].mean() / 60), 1)}}
    df = parse(ROOT / run / "out" / "slurmctld.log", every_s=3600)
    res["priority_rows"] = int(len(df))
    want = file_weights(name)
    got = {"Age": df["Age_w"], "QOS": df["QOS_w"], "Fairshare": df["Fairshare_w"],
           "JobSize": df["JobSize_w"], "TRES_cpu": df["TRES_cpu_w"], "TRES_mem": df["TRES_mem_w"]}
    if name == "B0":
        res["weights_match"] = bool(all((v == 0).all() for v in got.values()))
        return res
    # TRES weights only appear on lines whose factor is non-zero; compare where present.
    mismatch = {k: sorted(set(v[v != 0].round(2))) for k, v in got.items()
                if (v[v != 0].round(2) != round(want[k], 2)).any()}
    res["weights_match"] = not mismatch
    res["weight_mismatches"] = mismatch

    df = df.merge(jobs[["qos"]], left_on="job_id", right_index=True, how="left")
    factor_of = qos_factors(config["qos_priority"])
    if want["QOS"]:
        res["qos_factor_ok"] = bool((df["QOS_f"] - df["qos"].map(factor_of)).abs().max() < 1e-5)
    else:
        res["qos_factor_ok"] = bool((df["QOS_v"] == 0).all())

    snaps = df.groupby("snapshot").filter(lambda g: len(g) >= min_pending)
    res["snapshots_used"] = int(snaps["snapshot"].nunique())
    res["pending_per_snapshot_median"] = (
        float(snaps.groupby("snapshot").size().median()) if len(snaps) else 0.0)
    res["pending_class_mix"] = {q: round(float(v), 3) for q, v in
                                snaps["qos"].value_counts(normalize=True).items()}

    # 1) favored class first: share of cross-class pairs led by the more favored class
    pairs = wins = 0
    for _, g in snaps.groupby("snapshot"):
        pr, wn = class_dominance(g, factor_of)
        pairs, wins = pairs + pr, wins + wn
    res["class_dominance"] = round(wins / pairs, 4) if pairs else None

    # 2) then shortest time limit, then fairness: typical gaps between jobs of the same class
    within = []
    for (_, q), g in snaps.groupby(["snapshot", "qos"]):
        if len(g) >= 5:
            within.append({"fairness": gini_mean_difference(g["Fairshare_v"]),
                           "time": gini_mean_difference(g["JobSize_v"]), "n": len(g)})
    w = pd.DataFrame(within)
    res["within_class_gap"] = ({k: round(float(w[k].median()), 1) for k in ("fairness", "time")}
                               if len(w) else {})
    time_first = bool(len(w) and res["within_class_gap"]["time"] > res["within_class_gap"]["fairness"])
    strict = yaml.safe_load((ROOT / "config" / "weights.yaml").read_text())["tiers"]["size"] == "strict"
    need = 1.0 if strict else 0.9
    size_first = res["class_dominance"] is None or res["class_dominance"] >= need
    res["order_ok"] = bool(res["weights_match"] and res["qos_factor_ok"] and time_first and size_first)
    res["_snaps"] = snaps
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
    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / a.scenario / "jobs.parquet")
    jobs = jobs.set_index("job_id")
    events, accounts, n = slice_workload(a.scenario, a.hours, OUT / f"{a.tag}-input")
    print(f"{a.scenario}: first {a.hours:g} h, {n} jobs")
    with ThreadPoolExecutor(max_workers=len(CONFIGS)) as ex:
        runs = dict(zip(CONFIGS, ex.map(lambda c: run_one(c, a.tag, events, accounts,
                                                          a.time_start), CONFIGS)))

    results = [analyse(c, runs[c], cfg["configs"].get(c), jobs, a.min_pending, a.hours)
               for c in CONFIGS]
    pooled = pd.concat([r.pop("_snaps") for r in results if "_snaps" in r])
    fs_gap = pooled.groupby("snapshot")["Fairshare_f"].agg(gini_mean_difference)
    summary = {
        "scenario": a.scenario, "hours": a.hours, "jobs": n,
        "fairshare_factor_gap_median": float(fs_gap.median()) if len(fs_gap) else None,
        "configs": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"configs_{a.tag}.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    rows = [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            for r in results]
    pd.DataFrame(rows).to_csv(OUT / f"configs_{a.tag}.csv", index=False)
    for r in results:
        print(r["config"], "weights", r["weights_match"], "qos", r.get("qos_factor_ok"),
              "dominance", r.get("class_dominance"), "within", r.get("within_class_gap"),
              "order_ok", r.get("order_ok"), "cluster", r["cluster"], "problems", r["run_problems"])


if __name__ == "__main__":
    main()
