"""Calibrate factor scales and render the priority configuration files (SPEC §5).

Usage:
  .venv/bin/python -m sim.weights calibrate --from-check TAG      # pooled pending queues
  .venv/bin/python -m sim.weights calibrate --per-config-from TAG # each config's own queue
  .venv/bin/python -m sim.weights render   # writes config/priority/{B0,C0..C3,B1}.{conf,qos},
                                           # B1.static (SPEC §8.2), and B1's favored states
                                           # B1M, B1L (.conf, .qos, .static; SPEC §9.2)

Slurm factors (Slurm 23.11 priority/multifactor, single-node jobs on the 16-node cluster):
  QOS       = QOS priority / largest QOS priority (each job runs in its size class's QOS)
  Fairshare = Fair Tree factor (0..1), read from the priority debug log
  JobSize   = max(32, cpus) / T_min / 512   (SMALL_RELATIVE_TO_TIME, PriorityFavorSmall=NO;
              every single-node job counts as one node's 32 CPUs, so only T_min matters)
  TRES      = request / partition total (computed for reference; weight 0)
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / "config" / "weights.yaml"
SCALES = ROOT / "config" / "factor_scales.yaml"
PRIORITY_DIR = ROOT / "config" / "priority"
CLUSTER_CPUS, NODES, CLUSTER_MEM_MB = 512, 16, 2_048_000


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


def pending_factor_gaps(run: Path, jobs: pd.DataFrame, min_pending: int = 20,
                        within_class: bool = False, min_class: int = 5
                        ) -> dict[str, list[float]]:
    """Per-snapshot Gini mean difference of each factor across a run's pending jobs.

    within_class: measure among waiting jobs of the same size QOS (one value per snapshot and
    class with at least min_class jobs). With size strictly first, time limit and fairness
    only order jobs within a class, so that is where their gaps matter.
    """
    from sim.priority_log import parse   # local import: only needed here

    df = parse(run / "out" / "slurmctld.log", every_s=3600)
    picked = jobs.loc[df["job_id"]].reset_index()
    f = job_factors(picked)
    f["snapshot"] = df["snapshot"].to_numpy()
    f["fairshare"] = df["Fairshare_f"].to_numpy()
    f["qos"] = picked["qos"].to_numpy() if "qos" in picked else "all"
    gaps = {k: [] for k in ["tres_cpu", "tres_mem", "jobsize", "fairshare"]}
    for _, g in f.groupby("snapshot"):
        if len(g) < min_pending:
            continue
        groups = [h for _, h in g.groupby("qos") if len(h) >= min_class] if within_class else [g]
        for h in groups:
            for k in gaps:
                gaps[k].append(gini_mean_difference(h[k]))
    return gaps


def calibrate_per_config(tag: str, scenario: str = "K-s1", min_pending: int = 20) -> dict:
    """Give each configuration its own scales, measured on the queue it produced.

    A configuration reshapes its own queue: favoring short limits clears short jobs, so the
    time factor varies less among the jobs still waiting. Scales are measured among waiting
    jobs of the same size class, where time limit and fairness decide the order. Iterate
    (check, calibrate, render) until every configuration ranks them in order.
    """
    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / scenario / "jobs.parquet")
    jobs = jobs.set_index("job_id")
    scales = yaml.safe_load(SCALES.read_text())
    per = scales.get("per_config", {})
    for name in yaml.safe_load(WEIGHTS.read_text())["configs"]:
        gaps = pending_factor_gaps(ROOT / "results" / "check" / f"{tag}-{name}", jobs, min_pending,
                                   within_class=True)
        per[name] = {k: float(f"{np.median(v):.6g}") for k, v in gaps.items()}
        per[name]["samples"] = len(gaps["jobsize"])
        per[name]["source"] = (f"check '{tag}' on {scenario}: median over (snapshot, size class) "
                               f"groups of waiting jobs")
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


def qos_factors(qos_priority: dict[str, int]) -> dict[str, float]:
    """Slurm's normalized QOS factor: priority / largest QOS priority (0 if all are 0)."""
    top = max(qos_priority.values())
    return {q: (p / top if top else 0.0) for q, p in qos_priority.items()}


def size_step(qos_priority: dict[str, int]) -> float:
    """Smallest gap between distinct normalized QOS priorities (0 if all equal)."""
    levels = sorted(set(qos_factors(qos_priority).values()))
    gaps = [b - a for a, b in zip(levels, levels[1:])]
    return min(gaps) if gaps else 0.0


# Largest possible job-size factor: 32 / T_min / 512 with the shortest limit, 1 minute.
MAX_JOBSIZE_FACTOR = 32 / 1 / 512


def strict_size_bound(w: dict[str, int]) -> int:
    """Largest priority any job can get from everything except size (all factors <= 1)."""
    return math.ceil(w["JobSize"] * MAX_JOBSIZE_FACTOR + w["Fairshare"] + w["Age"])


def weights_for(config: dict, cfg: dict, scales: dict) -> dict[str, int]:
    """PriorityWeight* values for one configuration (TRES is not used: weight 0).

    tiers.size 'strict': one step between neighbouring size classes is worth more than the
    largest possible time + fairness + age total, so the favored class always comes first.
    A number instead: one step is worth that many points (soft tiers).
    """
    w = {
        "Age": cfg["age_weight"],
        "QOS": 0,
        "Fairshare": round(cfg["tiers"]["fairness"] / scales["fairshare"]),
        "JobSize": round(cfg["tiers"]["time"] / scales["jobsize"]),
        "TRES_CPU": 0, "TRES_Mem": 0,
    }
    step = size_step(config["qos_priority"])
    if step:
        per_step = (strict_size_bound(w) + 1 if cfg["tiers"]["size"] == "strict"
                    else cfg["tiers"]["size"])
        w["QOS"] = math.ceil(per_step / step)
    return w


B0_WEIGHTS = {"Age": 0, "QOS": 0, "Fairshare": 0, "JobSize": 0, "TRES_CPU": 0, "TRES_Mem": 0}


def conf_text(name: str, title: str, w: dict) -> str:
    return (f"# {name}: {title}\n"
            f"# Rendered from config/weights.yaml and config/factor_scales.yaml by sim/weights.py\n"
            f"PriorityWeightAge={w['Age']}\n"
            f"PriorityWeightQOS={w['QOS']}\n"
            f"PriorityWeightFairshare={w['Fairshare']}\n"
            f"PriorityWeightJobSize={w['JobSize']}\n"
            f"PriorityWeightTRES=CPU={w['TRES_CPU']},Mem={w['TRES_Mem']}\n")


def static_text(name: str, static: dict) -> str:
    """Static priority keys that replace the slurm.conf template's for this setting."""
    return (f"# {name}: static priority keys for sim/prepare_run.py; they replace the template's\n"
            f"# lines in sim/etc/slurm.conf.in (an empty value removes the key)\n" +
            "".join(f"{k}={'' if v is None else v}\n" for k, v in static.items()))


def qos_text(name: str, qos_priority: dict[str, int]) -> str:
    """QOS priorities for sim/prepare_run.py: one 'qos priority' pair per line."""
    return (f"# {name}: QOS priorities (Slurm normalizes by the largest)\n" +
            "".join(f"{q} {p}\n" for q, p in qos_priority.items()))


def validate(cfg: dict) -> None:
    """Every configuration gives a non-negative integer priority to each size QOS."""
    qos = set(cfg["size_qos"].values())
    seen = set()
    for name, c in cfg["configs"].items():
        pr = c["qos_priority"]
        if set(pr) != qos:
            raise ValueError(f"{name}: qos_priority must cover exactly {sorted(qos)}, got {sorted(pr)}")
        if any(not isinstance(v, int) or v < 0 for v in pr.values()):
            raise ValueError(f"{name}: QOS priorities must be non-negative integers, got {pr}")
        key = tuple(sorted(qos_factors(pr).items()))
        if key in seen:
            raise ValueError(f"{name}: duplicates another configuration")
        seen.add(key)


def validate_favored(name: str, qos_priority: dict[str, int], qos: set[str]) -> None:
    """A favored state raises exactly one size QOS, so its boost is the QOS weight itself."""
    if set(qos_priority) != qos:
        raise ValueError(f"{name}: qos_priority must cover exactly {sorted(qos)}")
    if any(not isinstance(v, int) or v < 0 for v in qos_priority.values()):
        raise ValueError(f"{name}: QOS priorities must be non-negative integers")
    if sum(v > 0 for v in qos_priority.values()) != 1:
        raise ValueError(f"{name}: exactly one QOS must have a non-zero priority")


def favored_states(cfg: dict) -> dict[str, tuple[str, dict]]:
    """{state: (reference it builds on, state spec)} for every reference's favored_on block."""
    return {s: (ref, spec) for ref, r in cfg.get("references", {}).items()
            for s, spec in r.get("favored_on", {}).get("states", {}).items()}


def render() -> dict[str, dict]:
    cfg = yaml.safe_load(WEIGHTS.read_text())
    scales = yaml.safe_load(SCALES.read_text())
    validate(cfg)
    PRIORITY_DIR.mkdir(parents=True, exist_ok=True)
    refs = cfg.get("references", {})
    names = ["B0"] + list(cfg["configs"]) + list(refs) + list(favored_states(cfg))
    keep = {".gitkeep"} | {f"{n}{ext}" for n in names for ext in (".conf", ".qos", ".static")}
    for stale in PRIORITY_DIR.iterdir():          # configurations no longer defined
        if stale.name not in keep:
            stale.unlink()
    zero_qos = {q: 0 for q in cfg["size_qos"].values()}
    (PRIORITY_DIR / "B0.conf").write_text(conf_text("B0", "FIFO baseline, all weights 0", B0_WEIGHTS))
    (PRIORITY_DIR / "B0.qos").write_text(qos_text("B0", zero_qos))
    out = {"B0": B0_WEIGHTS}
    for name, c in cfg["configs"].items():
        w = weights_for(c, cfg, scales_for(name, scales))
        out[name] = w
        title = f"{c['name']} (for: {c['scenario']}), then shortest time limit, then fairness"
        (PRIORITY_DIR / f"{name}.conf").write_text(conf_text(name, title, w))
        (PRIORITY_DIR / f"{name}.qos").write_text(qos_text(name, c["qos_priority"]))
    for name, r in refs.items():                   # SPEC §8.2: as published, not scaled
        w = {k: int(r["weights"][k]) for k in B0_WEIGHTS}
        out[name] = w
        (PRIORITY_DIR / f"{name}.conf").write_text(conf_text(name, r["name"], w))
        (PRIORITY_DIR / f"{name}.qos").write_text(qos_text(name, zero_qos))
        (PRIORITY_DIR / f"{name}.static").write_text(static_text(name, r.get("static", {})))
    for name, (ref, s) in favored_states(cfg).items():   # SPEC §9.2: reference + QOS boost
        validate_favored(name, s["qos_priority"], set(cfg["size_qos"].values()))
        r = refs[ref]
        w = {**out[ref], "QOS": int(r["favored_on"]["qos_weight"])}
        out[name] = w
        title = f"{s['name']}: {ref}'s weights plus a {w['QOS']}-point boost for one size class"
        (PRIORITY_DIR / f"{name}.conf").write_text(conf_text(name, title, w))
        (PRIORITY_DIR / f"{name}.qos").write_text(qos_text(name, s["qos_priority"]))
        (PRIORITY_DIR / f"{name}.static").write_text(static_text(name, r.get("static", {})))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("action", choices=["calibrate", "render"])
    p.add_argument("--from-check", metavar="TAG",
                   help="calibrate on the pooled pending queues of check run TAG "
                        "(sim/check_configs.py)")
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
            p.error("calibrate needs --from-check TAG or --per-config-from TAG")
        print(yaml.safe_dump(out, sort_keys=False))
    else:
        for name, w in render().items():
            print(name, w)


if __name__ == "__main__":
    main()
