"""Task 1.6: calibrate factor scales and render the priority weight files (SPEC §5).

Usage:
  .venv/bin/python -m sim.weights calibrate                       # first pass: all jobs of K
  .venv/bin/python -m sim.weights calibrate --from-check TAG      # pooled pending queues
  .venv/bin/python -m sim.weights calibrate --per-config-from TAG # each config's own queue
  .venv/bin/python -m sim.weights render      # writes config/priority/{B0,C1..C6}.conf

Slurm factors (Slurm 23.11 priority/multifactor, single-node jobs on the 16-node cluster):
  TRES cpu  = cpus / 512                 TRES mem = mem_mb / 2,048,000
  JobSize   = max(32, cpus) / T_min / 512   (SMALL_RELATIVE_TO_TIME, PriorityFavorSmall=NO;
              every single-node job counts as one node's 32 CPUs, so only T_min matters)
  Fairshare = Fair Tree factor (0..1); its scale comes from the priority debug log (task 1.7).
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / "config" / "weights.yaml"
SCALES = ROOT / "config" / "factor_scales.yaml"
PRIORITY_DIR = ROOT / "config" / "priority"
CLUSTER_CPUS, NODES, CLUSTER_MEM_MB = 512, 16, 2_048_000
PROVISIONAL_FAIRSHARE_SCALE = 0.3


def gini_mean_difference(x) -> float:
    """Mean |x_i - x_j| over all pairs i != j (exact, O(n log n))."""
    x = np.sort(np.asarray(x, dtype=float))
    n = len(x)
    if n < 2:
        return 0.0
    i = np.arange(1, n + 1)
    return float(2 * np.sum((2 * i - n - 1) * x) / (n * (n - 1)))


def job_factors(jobs: pd.DataFrame) -> pd.DataFrame:
    """Per-job TRES and job-size factors as Slurm computes them for this cluster."""
    cpus = jobs["cpus"].astype(float)
    t = jobs["timelimit_min"].astype(float)
    per_node = CLUSTER_CPUS / NODES
    return pd.DataFrame({
        "tres_cpu": cpus / CLUSTER_CPUS,
        "tres_mem": jobs["mem_mb"].astype(float) / CLUSTER_MEM_MB,
        "jobsize": (np.maximum(per_node, cpus) / t / CLUSTER_CPUS).clip(0, 1),
    })


def calibrate(scenario: str = "K", seeds=(1, 2, 3), fairshare: float | None = None,
              fairshare_source: str | None = None) -> dict:
    """Measure the scale of each per-job factor on scenario K and write factor_scales.yaml."""
    jobs = pd.concat([pd.read_parquet(ROOT / "workloads" / "traces" / f"{scenario}-s{s}" /
                                      "jobs.parquet") for s in seeds])
    f = job_factors(jobs)
    old = yaml.safe_load(SCALES.read_text()) if SCALES.exists() else {}
    if fairshare is None:
        fairshare = old.get("fairshare", PROVISIONAL_FAIRSHARE_SCALE)
        fairshare_source = old.get("sources", {}).get("fairshare",
                                                       "provisional until task 1.7")
    scales = {k: gini_mean_difference(f[k]) for k in ["tres_cpu", "tres_mem", "jobsize"]}
    out = {
        **{k: float(f"{v:.6g}") for k, v in scales.items()},
        "fairshare": float(f"{fairshare:.6g}"),
        "sources": {
            "tres_cpu": f"Gini mean difference over {len(jobs)} jobs of {scenario} seeds {list(seeds)}",
            "tres_mem": f"Gini mean difference over {len(jobs)} jobs of {scenario} seeds {list(seeds)}",
            "jobsize": f"Gini mean difference over {len(jobs)} jobs of {scenario} seeds {list(seeds)}",
            "fairshare": fairshare_source,
        },
        "correlation_U_L": float(f"{np.corrcoef(0.5 * f['tres_cpu'] / scales['tres_cpu'] + 0.5 * f['tres_mem'] / scales['tres_mem'], f['jobsize'] / scales['jobsize'])[0, 1]:.4f}"),
    }
    SCALES.write_text("# Measured factor scales (Gini mean difference). Written by\n"
                      "#   .venv/bin/python -m sim.weights calibrate\n" +
                      yaml.safe_dump(out, sort_keys=False))
    return out


def pending_factor_gaps(run: Path, jobs: pd.DataFrame, min_pending: int = 20
                        ) -> dict[str, list[float]]:
    """Per-snapshot Gini mean difference of each factor across a run's pending jobs."""
    from sim.priority_log import parse   # local import: only needed here

    df = parse(run / "out" / "slurmctld.log", every_s=3600)
    f = job_factors(jobs.loc[df["job_id"]].reset_index())
    f["snapshot"] = df["snapshot"].to_numpy()
    f["fairshare"] = df["Fairshare_f"].to_numpy()
    gaps = {k: [] for k in ["tres_cpu", "tres_mem", "jobsize", "fairshare"]}
    for _, g in f.groupby("snapshot"):
        if len(g) < min_pending:
            continue
        for k in gaps:
            gaps[k].append(gini_mean_difference(g[k]))
    return gaps


def calibrate_per_config(tag: str, scenario: str = "K-s1", min_pending: int = 20) -> dict:
    """Give each of C1-C6 its own scales, measured on the queue that configuration produced.

    The pooled scales fit some configurations' queues but not others: a configuration that
    favors short jobs clears them from the queue, so the job-size factor varies less among
    the jobs still waiting. Measuring each configuration's scales in its own check run makes
    the tier gaps hold in the queue it actually creates. Iterate (check, calibrate, render)
    until every configuration ranks its goals in order.
    """
    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / scenario / "jobs.parquet")
    jobs = jobs.set_index("job_id")
    scales = yaml.safe_load(SCALES.read_text())
    per = scales.get("per_config", {})
    for name in yaml.safe_load(WEIGHTS.read_text())["configs"]:
        gaps = pending_factor_gaps(ROOT / "results" / "check" / f"{tag}-{name}", jobs, min_pending)
        per[name] = {k: float(f"{np.median(v):.6g}") for k, v in gaps.items()}
        per[name]["snapshots"] = len(gaps["jobsize"])
        per[name]["source"] = f"check '{tag}' on {scenario}"
    scales["per_config"] = per
    SCALES.write_text("# Measured factor scales (Gini mean difference among pending jobs).\n"
                      "# Top level: pooled over all configurations (calibrate --from-check TAG).\n"
                      "# per_config: each configuration's own queue (calibrate --per-config-from TAG).\n" +
                      yaml.safe_dump(scales, sort_keys=False))
    return per


def scales_for(name: str, scales: dict) -> dict:
    """Scales a configuration uses: its own if measured, else the pooled ones."""
    return {**scales, **scales.get("per_config", {}).get(name, {})}


def calibrate_pending(tag: str, scenario: str = "K-s1", min_pending: int = 20) -> dict:
    """Measure all four scales on the pending queues of a check run (sim/check_configs.py).

    Priority only orders jobs that are waiting, and the waiting queue is not a random sample
    of the workload: large jobs wait longer and short-limit jobs backfill quickly. So scales
    are the median, over hourly queue snapshots of every configuration in the check (B0 and
    C1-C6 pooled), of each factor's Gini mean difference across the pending jobs. TRES and
    job-size factors are computed from each pending job's request (they match Slurm's logged
    values to print precision); fair share is read from the log (C1-C6 only, since Slurm
    skips factors whose weight is 0).
    """
    from sim.priority_log import parse   # local import: only needed here

    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / scenario / "jobs.parquet")
    jobs = jobs.set_index("job_id")
    gaps = {k: [] for k in ["tres_cpu", "tres_mem", "jobsize", "fairshare"]}
    runs = sorted((ROOT / "results" / "check").glob(f"{tag}-[BC][0-9]"))
    for run in runs:
        df = parse(run / "out" / "slurmctld.log", every_s=3600)
        f = job_factors(jobs.loc[df["job_id"]].reset_index())
        f["snapshot"] = df["snapshot"].to_numpy()
        f["fairshare"] = df["Fairshare_f"].to_numpy()
        for _, g in f.groupby("snapshot"):
            if len(g) < min_pending:
                continue
            for k in ["tres_cpu", "tres_mem", "jobsize"]:
                gaps[k].append(gini_mean_difference(g[k]))
            if not run.name.endswith("B0"):
                gaps["fairshare"].append(gini_mean_difference(g["fairshare"]))
    scales = {k: float(np.median(v)) for k, v in gaps.items()}
    src = (f"median over {len(gaps['tres_cpu'])} hourly pending-queue snapshots "
           f"(>= {min_pending} jobs) of check '{tag}' on {scenario}: "
           f"{', '.join(r.name for r in runs)}")
    out = {**{k: float(f"{v:.6g}") for k, v in scales.items()},
           "sources": {k: src + (" (C1-C6 only)" if k == "fairshare" else "") for k in scales},
           "snapshots": {k: len(v) for k, v in gaps.items()}}
    SCALES.write_text("# Measured factor scales (Gini mean difference among pending jobs). Written by\n"
                      f"#   .venv/bin/python -m sim.weights calibrate --from-check {tag}\n" +
                      yaml.safe_dump(out, sort_keys=False))
    return out


def weights_for(order: list[str], cfg: dict, scales: dict) -> dict[str, float]:
    """PriorityWeight* values for one ordering of the goals."""
    w = {"Age": cfg["age_weight"], "Fairshare": 0, "JobSize": 0, "TRES_CPU": 0, "TRES_Mem": 0}
    for tier, goal in zip(cfg["tiers"], order):
        factor = cfg["goals"][goal]["factor"]
        if factor == "fairshare":
            w["Fairshare"] = round(tier / scales["fairshare"])
        elif factor == "jobsize":
            w["JobSize"] = round(tier / scales["jobsize"])
        elif factor == "tres":
            split = cfg["goals"][goal]["split"]
            w["TRES_CPU"] = round(split["cpu"] * tier / scales["tres_cpu"])
            w["TRES_Mem"] = round(split["mem"] * tier / scales["tres_mem"])
    return w


def conf_text(name: str, title: str, w: dict) -> str:
    return (f"# {name}: {title}\n"
            f"# Rendered from config/weights.yaml and config/factor_scales.yaml by sim/weights.py\n"
            f"PriorityWeightAge={w['Age']}\n"
            f"PriorityWeightFairshare={w['Fairshare']}\n"
            f"PriorityWeightJobSize={w['JobSize']}\n"
            f"PriorityWeightTRES=CPU={w['TRES_CPU']},Mem={w['TRES_Mem']}\n")


def render() -> dict[str, dict]:
    cfg = yaml.safe_load(WEIGHTS.read_text())
    scales = yaml.safe_load(SCALES.read_text())
    orders = list(cfg["configs"].values())
    if sorted(map(tuple, orders)) != sorted(itertools.permutations(cfg["goals"])):
        raise ValueError("configs must be exactly the orderings of the goals")
    short = {"fairness": "F", "utilization": "U", "load_balance": "L"}
    out = {"B0": {"Age": 0, "Fairshare": 0, "JobSize": 0, "TRES_CPU": 0, "TRES_Mem": 0}}
    PRIORITY_DIR.mkdir(parents=True, exist_ok=True)
    (PRIORITY_DIR / "B0.conf").write_text(conf_text("B0", "FIFO baseline, all weights 0", out["B0"]))
    for name, order in cfg["configs"].items():
        w = weights_for(order, cfg, scales_for(name, scales))
        out[name] = w
        title = " > ".join(short[g] for g in order)
        (PRIORITY_DIR / f"{name}.conf").write_text(conf_text(name, title, w))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("action", choices=["calibrate", "render"])
    p.add_argument("--from-check", metavar="TAG",
                   help="calibrate on the pending queues of check run TAG (sim/check_configs.py) "
                        "instead of on all jobs of scenario K")
    p.add_argument("--per-config-from", metavar="TAG",
                   help="give each configuration its own scales, measured in check run TAG")
    p.add_argument("--scenario", default="K-s1", help="scenario the check ran")
    a = p.parse_args()
    if a.action == "calibrate":
        if a.per_config_from:
            out = calibrate_per_config(a.per_config_from, a.scenario)
        elif a.from_check:
            out = calibrate_pending(a.from_check, a.scenario)
        else:
            out = calibrate()
        print(yaml.safe_dump(out, sort_keys=False))
    else:
        for name, w in render().items():
            print(name, w)


if __name__ == "__main__":
    main()
