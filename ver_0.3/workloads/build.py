"""Task 1.5: build workload scenarios (SPEC §3) from the trace.

Usage:  .venv/bin/python -m workloads.build [SCENARIO ...] [--seeds 1 2 3]

For each scenario and seed, writes workloads/traces/<scenario>-s<seed>/ (gitignored):
  jobs.parquet   one row per job (SPEC §3.3 fields, plus job_id/name and X segment)
  events.txt     simulator job-trace lines, job names jobid_1001.. in submission order
  accounts.txt   account<TAB>user associations for sim/prepare_run.py
  manifest.json  source, filters, seed, thinning, target and achieved load, class counts, hashes
and a summary to results/workloads/summary.csv (+ X segment table).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from workloads import trace
from workloads.classify import classify, load_config

ROOT = trace.ROOT
CONFIG = ROOT / "config" / "scenarios.yaml"
TRACES = ROOT / "workloads" / "traces"
SUMMARY = ROOT / "results" / "workloads"
DAY_S = 86_400
CAP_CPU = trace.CLUSTER_CORES        # cores
CAP_MEM = trace.CLUSTER_MEM_MB       # MB


# --------------------------------------------------------------------------- source jobs

def load_jobs() -> pd.DataFrame:
    """All filtered trace jobs with the fields the builder needs."""
    cols = ["jobid", "state", "account", "user", "partition", "submit", "timelimit", "elapsed",
            "allocmem", "alloccpus", "allocgpus", "started", "hostnames"]
    frames = []
    for m in trace.months():
        df = trace.load(cols, [m])
        df = df[trace.filter_mask(df)]
        frames.append(pd.DataFrame({
            "src_jobid": df["jobid"].astype("int64"),
            "src_state": df["state"],
            "user": df["user"],
            "account": df["account"],
            "submit": df["submit"].astype("int64"),
            "cpus": df["alloccpus"].astype("int64"),
            "mem_mb": df["allocmem"].astype("int64"),
            "timelimit_s": df["timelimit"].astype("int64"),
            "runtime_s": trace.runtime_s(df),
        }))
    jobs = pd.concat(frames, ignore_index=True)
    for c in ["src_state", "user", "account"]:
        jobs[c] = jobs[c].astype("category")
    jobs["class"] = classify(jobs["cpus"], jobs["mem_mb"], jobs["timelimit_s"])
    jobs["week"] = trace.week_of(jobs["submit"])
    jobs["tod"] = (jobs["submit"] - trace.WEEK0) % DAY_S
    jobs["cpu_s"] = jobs["cpus"] * jobs["runtime_s"]
    jobs["mem_s"] = jobs["mem_mb"] * jobs["runtime_s"]
    return jobs


# --------------------------------------------------------------------------- thinning

def rho(cpu_s: float, mem_s: float, window_s: float) -> float:
    return max(cpu_s / (CAP_CPU * window_s), mem_s / (CAP_MEM * window_s))


def thin(cand: pd.DataFrame, rng: np.random.Generator, target: float, window_s: float,
         base_cpu_s: float = 0.0, base_mem_s: float = 0.0, quota: int | None = None,
         min_jobs: int = 0) -> tuple[pd.DataFrame, float]:
    """Keep a seeded random subset so that load over window_s is as close to target as possible.

    Jobs are ordered by a seeded uniform draw u; the kept set is every job with u below a
    threshold p (Bernoulli thinning with a solved p). base_* is load already in the window.
    With quota, the first `quota` jobs of each class (in u order) are always kept.
    Returns (kept jobs, p).
    """
    if len(cand) == 0:
        return cand, 0.0
    u = rng.random(len(cand))
    order = np.argsort(u, kind="stable")
    c = cand.iloc[order]
    us = u[order]
    forced = np.zeros(len(c), dtype=bool)
    if quota:
        rank = c.groupby("class", observed=True).cumcount().to_numpy()
        forced = rank < quota
    fcpu = base_cpu_s + c["cpu_s"].to_numpy()[forced].sum()
    fmem = base_mem_s + c["mem_s"].to_numpy()[forced].sum()
    rest = ~forced
    cum_cpu = fcpu + np.cumsum(np.where(rest, c["cpu_s"].to_numpy(), 0))
    cum_mem = fmem + np.cumsum(np.where(rest, c["mem_s"].to_numpy(), 0))
    r = np.maximum(cum_cpu / (CAP_CPU * window_s), cum_mem / (CAP_MEM * window_s))
    r0 = rho(fcpu, fmem, window_s)
    k = int(np.searchsorted(r, target))          # first prefix length-1 reaching target
    if k >= len(r):
        k = len(r) - 1                            # not enough jobs: keep all
    elif k > 0 and abs(r[k - 1] - target) < abs(r[k] - target):
        k -= 1
    if k == 0 and abs(r0 - target) < abs(r[0] - target):
        n_keep = 0
    else:
        n_keep = k + 1
    n_keep = max(n_keep, min(min_jobs, len(r)))
    keep = forced.copy()
    keep[:n_keep] |= rest[:n_keep]
    p = float(us[n_keep - 1]) if n_keep else 0.0
    return c[keep], p


def rng_for(seed: int, scenario: str, part: int) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32(scenario.encode()), part])


# --------------------------------------------------------------------------- scenarios

def natural_week(jobs: pd.DataFrame, name: str, week: int, seed: int, target: float,
                 quota: int | None) -> tuple[pd.DataFrame, dict]:
    start = trace.WEEK0 + week * trace.WEEK_S
    cand = jobs[jobs["week"] == week]
    kept, p = thin(cand, rng_for(seed, name, 0), target, trace.WEEK_S, quota=quota)
    kept = kept.assign(submit_s=kept["submit"] - start, segment="")
    info = {"source_window_utc": [iso(start), iso(start + trace.WEEK_S)], "p": p,
            "candidates": int(len(cand))}
    return kept, info


def segment_candidates(pool: pd.DataFrame, spec: dict) -> pd.DataFrame:
    cand = pool
    if "classes" in spec:
        cand = cand[cand["class"].isin(spec["classes"])]
    if spec.get("memory_bound"):
        cand = cand[cand["mem_mb"] * trace.NODE_CORES > cand["cpus"] * trace.NODE_MEM_MB]
    return cand


def top_account(pool: pd.DataFrame) -> str:
    load = pool.groupby("account", observed=True)["mem_s"].sum()
    return str(load.idxmax())


def shifting_week(jobs: pd.DataFrame, name: str, cfg: dict, seed: int, target: float
                  ) -> tuple[pd.DataFrame, dict]:
    """Scenario X: one segment per day, each thinned to target in its own binding resource.

    Segments alternate between CPU-bound demand (small-short flood) and memory-bound demand
    (large-long, memory-heavy), so the week's load in either single resource ends up below
    target even though every segment is at target in the resource it saturates.
    """
    pool = jobs[jobs["week"].isin(cfg["pool_weeks"])]
    segs = cfg["segments"]
    burst_account = top_account(pool)
    parts, seg_info = {}, []

    def build_block(i_seg, spec, t0, t1, part):
        tod0, tod1 = t0 % DAY_S, (t1 - 1) % DAY_S + 1
        in_block = pool[(pool["tod"] >= tod0) & (pool["tod"] < tod1)]
        window = t1 - t0
        if "burst_share" in spec:
            # The burst account supplies `share` of the target; other accounts fill the rest.
            a = in_block[in_block["account"] == burst_account]
            o = in_block[in_block["account"] != burst_account]
            ka, _ = thin(a, rng_for(seed, name, part), target * spec["burst_share"], window)
            ko, _ = thin(o, rng_for(seed, name, part + 100), target, window,
                         base_cpu_s=ka["cpu_s"].sum(), base_mem_s=ka["mem_s"].sum())
            kept = pd.concat([ka, ko])
        else:
            kept, _ = thin(segment_candidates(in_block, spec), rng_for(seed, name, part),
                           target, window)
        day = t0 // DAY_S
        return kept.assign(submit_s=day * DAY_S + kept["tod"], segment=f"{i_seg}:{spec['name']}")

    for i, spec in enumerate(segs):
        t0, t1 = i * DAY_S, (i + 1) * DAY_S
        if "alternate" in spec:
            step = int(spec["block_hours"] * 3600)
            blocks = []
            for b, s in enumerate(range(t0, t1, step)):
                sub = dict(segs[spec["alternate"][b % len(spec["alternate"])]])
                sub["name"] = spec["name"]
                blocks.append(build_block(i, sub, s, min(s + step, t1), 1000 * i + b))
            parts[i] = pd.concat(blocks)
        else:
            parts[i] = build_block(i, spec, t0, t1, 1000 * i)

    kept = pd.concat([parts[i] for i in range(len(segs))])
    for i in range(len(segs)):
        k = parts[i]
        seg_info.append({"segment": i, "name": segs[i]["name"], "jobs": int(len(k)),
                         "rho_cpu": k["cpu_s"].sum() / (CAP_CPU * DAY_S),
                         "rho_mem": k["mem_s"].sum() / (CAP_MEM * DAY_S)})
    info = {"pool_weeks": cfg["pool_weeks"], "burst_account": burst_account,
            "segments": seg_info}
    return kept, info


def place_window(pool: pd.DataFrame, t0: int, t1: int, rho_target: float, seed: int, name: str,
                 part: int, label: str, min_jobs: int = 0) -> pd.DataFrame:
    """Thin pool jobs whose time of day falls in [t0, t1) to rho_target and re-time them there.

    Windows that cross midnight are split at the day boundary; each piece is thinned on its
    own length, so load is spread evenly over the window.
    """
    pieces = []
    t = t0
    while t < t1:
        end = min(t1, (t // DAY_S + 1) * DAY_S)
        tod0, tod1 = t % DAY_S, (end - 1) % DAY_S + 1
        cand = pool[(pool["tod"] >= tod0) & (pool["tod"] < tod1)]
        kept, _ = thin(cand, rng_for(seed, name, part * 10 + len(pieces)), rho_target, end - t,
                       min_jobs=min_jobs if not pieces else 0)
        pieces.append(kept.assign(submit_s=(t // DAY_S) * DAY_S + kept["tod"], segment=label))
        t = end
    return pd.concat(pieces) if pieces else pool.iloc[:0]


def overlay_week(jobs: pd.DataFrame, name: str, cfg: dict, seed: int, target: float
                 ) -> tuple[pd.DataFrame, dict]:
    """Scenario Y: natural background all week plus overlapping bursts at random times.

    The background is thinned to base_share * target per day. Each burst is a window of
    burst_hours, starting at a seeded random whole hour, filled with one pattern's jobs at
    burst_share * target. Patterns are used in turn from a seeded order, so each appears about
    equally often. Bursts may overlap (load above target) or leave gaps (load below).
    """
    pool = jobs[jobs["week"].isin(cfg["pool_weeks"])]
    burst_account = top_account(pool)
    rng = rng_for(seed, name, 999_999)
    week_h = trace.WEEK_S // 3600

    parts = [place_window(pool, d * DAY_S, (d + 1) * DAY_S, target * cfg["base_share"], seed,
                          name, 1 + d, "base") for d in range(7)]
    patterns = cfg["patterns"]
    order = rng.permutation(len(patterns))
    lo, hi = cfg["burst_hours"]
    bursts = []
    for b in range(cfg["burst_count"]):
        spec = patterns[order[b % len(patterns)]]
        hours = int(rng.integers(lo, hi + 1))
        start_h = int(rng.integers(0, week_h - hours + 1))
        cand = segment_candidates(pool, spec)
        if spec.get("account") == "top":
            cand = cand[cand["account"] == burst_account]
        label = f"burst{b:02d}:{spec['name']}"
        # At least one job per burst: one multi-day large job can exceed a short burst's
        # whole target, and thinning would otherwise keep nothing.
        k = place_window(cand, start_h * 3600, (start_h + hours) * 3600,
                         target * cfg["burst_share"], seed, name, 100 + b, label, min_jobs=1)
        parts.append(k)
        bursts.append({"burst": b, "name": spec["name"], "start_h": start_h, "hours": hours,
                       "jobs": int(len(k)),
                       "rho_cpu": k["cpu_s"].sum() / (CAP_CPU * hours * 3600),
                       "rho_mem": k["mem_s"].sum() / (CAP_MEM * hours * 3600)})

    active = np.zeros(week_h, dtype=int)
    for bi in bursts:
        active[bi["start_h"]:bi["start_h"] + bi["hours"]] += 1
    info = {"pool_weeks": cfg["pool_weeks"], "burst_account": burst_account,
            "base_share": cfg["base_share"], "burst_share": cfg["burst_share"],
            "hours_with_0_bursts": int((active == 0).sum()),
            "hours_with_1_burst": int((active == 1).sum()),
            "hours_with_2plus_bursts": int((active >= 2).sum()),
            "bursts": bursts}
    return pd.concat(parts), info


# --------------------------------------------------------------------------- output

def iso(ts: int) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


def finalize(kept: pd.DataFrame) -> pd.DataFrame:
    """Order by submission, number from 1001, and keep the SPEC §3.3 fields."""
    out = kept.sort_values(["submit_s", "src_jobid"], kind="stable").reset_index(drop=True)
    out.insert(0, "job_id", np.arange(1001, 1001 + len(out)))
    out.insert(1, "name", "jobid_" + out["job_id"].astype(str))
    out["timelimit_min"] = -(-out["timelimit_s"] // 60)
    cols = ["job_id", "name", "submit_s", "runtime_s", "timelimit_min", "cpus", "mem_mb",
            "user", "account", "class", "src_jobid", "src_state", "segment"]
    out = out[cols].copy()
    for c in ["user", "account", "src_state"]:
        out[c] = out[c].astype(str)
    out["class"] = pd.Categorical(out["class"].astype(str), categories=load_config()["classes"],
                                  ordered=True)
    return out


def event_line(r) -> str:
    return (f"-dt {r.submit_s} -e submit_batch_job | -J {r.name} -sim-walltime {r.runtime_s} "
            f"--uid={r.user} -t {r.timelimit_min} -n {r.cpus} --ntasks-per-node={r.cpus} -N 1 "
            f"--mem={r.mem_mb} -A {r.account} -p normal -q normal pseudo.job")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                       text=True)
    return r.stdout.strip()


def write_scenario(df: pd.DataFrame, name: str, seed: int, target: float, info: dict,
                   quota: int | None) -> dict:
    d = TRACES / f"{name}-s{seed}"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / "jobs.parquet", index=False)
    (d / "events.txt").write_text("\n".join(event_line(r) for r in df.itertuples()) + "\n")
    pairs = sorted(set(zip(df["account"], df["user"])))
    (d / "accounts.txt").write_text("# account\tuser\n" + "".join(f"{a}\t{u}\n" for a, u in pairs))

    cpu_s = float((df["cpus"] * df["runtime_s"]).sum())
    mem_s = float((df["mem_mb"] * df["runtime_s"]).sum())
    manifest = {
        "scenario": name, "seed": seed, "built_from_commit": git_commit(),
        "filters": {"partitions": list(trace.KEEP_PARTITIONS), "gpus": 0, "started": 1,
                    "single_node": True, "max_cpus": trace.NODE_CORES,
                    "max_mem_mb": trace.NODE_MEM_MB, "max_timelimit_s": trace.MAX_TIMELIMIT_S},
        "target_rho": target, "class_quota": quota,
        "achieved": {"rho_cpu": cpu_s / (CAP_CPU * trace.WEEK_S),
                     "rho_mem": mem_s / (CAP_MEM * trace.WEEK_S)},
        "jobs": int(len(df)), "users": int(df["user"].nunique()),
        "accounts": int(df["account"].nunique()), "associations": len(pairs),
        "class_counts": {k: int(v) for k, v in df["class"].value_counts(sort=False).items()},
        **info,
        "sha256": {f: sha256(d / f) for f in ["jobs.parquet", "events.txt", "accounts.txt"]},
    }
    manifest["achieved"]["rho"] = max(manifest["achieved"].values())
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, default=float) + "\n")
    return manifest


def build(names: list[str] | None = None, seeds: list[int] | None = None) -> list[dict]:
    cfg = yaml.safe_load(CONFIG.read_text())
    target, quota = cfg["target_rho"], cfg.get("class_quota")
    seeds = seeds or cfg["seeds"]
    all_names = list(cfg["natural"]) + list(cfg["shifting"]) + list(cfg.get("overlay", {}))
    names = names or all_names
    jobs = load_jobs()
    manifests = []
    for name in names:
        for seed in seeds:
            if name in cfg["natural"]:
                kept, info = natural_week(jobs, name, cfg["natural"][name]["week"], seed, target,
                                          quota)
            elif name in cfg["shifting"]:
                kept, info = shifting_week(jobs, name, cfg["shifting"][name], seed, target)
            else:
                kept, info = overlay_week(jobs, name, cfg["overlay"][name], seed, target)
            m = write_scenario(finalize(kept), name, seed, target, info, quota)
            manifests.append(m)
            a = m["achieved"]
            print(f"{name}-s{seed}: {m['jobs']:,} jobs, {m['accounts']} accounts, "
                  f"rho_cpu={a['rho_cpu']:.3f} rho_mem={a['rho_mem']:.3f}")
    write_summary(manifests)
    return manifests


def write_summary(manifests: list[dict]) -> None:
    SUMMARY.mkdir(parents=True, exist_ok=True)
    rows, segs, bursts = [], [], []
    for m in manifests:
        row = {"scenario": m["scenario"], "seed": m["seed"], "jobs": m["jobs"],
               "users": m["users"], "accounts": m["accounts"],
               "rho_cpu": m["achieved"]["rho_cpu"], "rho_mem": m["achieved"]["rho_mem"],
               "p": m.get("p")}
        row.update(m["class_counts"])
        rows.append(row)
        for s in m.get("segments", []):
            segs.append({"scenario": m["scenario"], "seed": m["seed"], **s})
        for b in m.get("bursts", []):
            bursts.append({"scenario": m["scenario"], "seed": m["seed"], **b})
    path = SUMMARY / "summary.csv"
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path)
        keep = ~old.set_index(["scenario", "seed"]).index.isin(new.set_index(["scenario", "seed"]).index)
        if keep.any():
            new = pd.concat([old[keep], new], ignore_index=True)
    new.sort_values(["scenario", "seed"]).to_csv(path, index=False, float_format="%.4f")
    if segs:
        pd.DataFrame(segs).to_csv(SUMMARY / "X_segments.csv", index=False, float_format="%.4f")
    if bursts:
        pd.DataFrame(bursts).to_csv(SUMMARY / "Y_bursts.csv", index=False, float_format="%.4f")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--seeds", nargs="*", type=int)
    a = p.parse_args()
    build(a.scenarios or None, a.seeds or None)


if __name__ == "__main__":
    main()
