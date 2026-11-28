"""Task 1.5: build workload scenarios (SPEC §3) from the trace.

Usage:  .venv/bin/python -m workloads.build [SCENARIO ...] [--seeds 1 2 3]
        (a day scenario's name, D, builds all its patterns D-nat, D-flood, ...)

For each scenario and seed, writes workloads/traces/<scenario>-s<seed>/ (gitignored):
  jobs.parquet   one row per job (SPEC §3.3 fields, plus job_id/name and X segment)
  events.txt     simulator job-trace lines, job names jobid_1001.. in submission order
  accounts.txt   account<TAB>user associations for sim/prepare_run.py
  manifest.json  source, filters, seed, thinning, target and achieved load, class counts, hashes
and summaries to results/workloads/: summary.csv, plus X_segments.csv, Y_bursts.csv,
D_days.csv and D_bursts.csv for the scenarios that have them.
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
WEIGHTS = ROOT / "config" / "weights.yaml"   # size class -> QOS label
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


def segment_block(pool: pd.DataFrame, spec: dict, t0: int, t1: int, seed: int, name: str,
                  part: int, target: float, burst_account: str, label: str) -> pd.DataFrame:
    """One pattern block [t0, t1) inside one day (scenarios X and D).

    Takes the pool jobs whose time of day falls in the block, filters them by the pattern,
    thins them to target over the block and re-times them to (day of t0) + time of day.
    """
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
    return kept.assign(submit_s=day * DAY_S + kept["tod"], segment=label)


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
        return segment_block(pool, spec, t0, t1, seed, name, part, target, burst_account,
                             f"{i_seg}:{spec['name']}")

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


def place_bursts(pool: pd.DataFrame, cfg: dict, span_h: int, offset_h: int, seed: int,
                 name: str, target: float, burst_account: str, rng: np.random.Generator
                 ) -> tuple[list[pd.DataFrame], list[dict]]:
    """Bursts of cfg['patterns'] at seeded random whole hours in [offset_h, offset_h + span_h).

    Patterns are used in turn from a seeded order. Each burst lasts a seeded number of hours
    in cfg['burst_hours'] and adds cfg['burst_share'] * target while it runs. start_h in the
    returned table is relative to offset_h. Used by scenario Y (the week) and D-over (one day).
    """
    patterns = cfg["patterns"]
    order = rng.permutation(len(patterns))
    lo, hi = cfg["burst_hours"]
    parts, bursts = [], []
    for b in range(cfg["burst_count"]):
        spec = patterns[order[b % len(patterns)]]
        hours = int(rng.integers(lo, hi + 1))
        start_h = int(rng.integers(0, span_h - hours + 1))
        cand = segment_candidates(pool, spec)
        if spec.get("account") == "top":
            cand = cand[cand["account"] == burst_account]
        label = f"burst{b:02d}:{spec['name']}"
        t0 = (offset_h + start_h) * 3600
        # At least one job per burst: one multi-day large job can exceed a short burst's
        # whole target, and thinning would otherwise keep nothing.
        k = place_window(cand, t0, t0 + hours * 3600, target * cfg["burst_share"], seed, name,
                         100 + b, label, min_jobs=1)
        parts.append(k)
        bursts.append({"burst": b, "name": spec["name"], "start_h": start_h, "hours": hours,
                       "jobs": int(len(k)),
                       "rho_cpu": k["cpu_s"].sum() / (CAP_CPU * hours * 3600),
                       "rho_mem": k["mem_s"].sum() / (CAP_MEM * hours * 3600)})
    return parts, bursts


def place_blocks(pool: pd.DataFrame, cfg: dict, t0: int, t1: int, seed: int, name: str,
                 target: float, burst_account: str) -> tuple[list[pd.DataFrame], list[dict]]:
    """Blocks of block_hours from t0 to t1, cycling through cfg['patterns'] in order; each
    adds cfg['burst_share'] * target while it runs (D-alt). Labelled 'burstNN:<name>' like Y's
    bursts, and listed in the same table form; start_h is relative to t0."""
    step = int(cfg["block_hours"] * 3600)
    parts, blocks = [], []
    for b, s in enumerate(range(t0, t1, step)):
        spec = cfg["patterns"][b % len(cfg["patterns"])]
        cand = segment_candidates(pool, spec)
        if spec.get("account") == "top":
            cand = cand[cand["account"] == burst_account]
        e = min(s + step, t1)
        label = f"burst{b:02d}:{spec['name']}"
        k = place_window(cand, s, e, target * cfg["burst_share"], seed, name, 100 + b, label,
                         min_jobs=1)
        parts.append(k)
        hours = (e - s) / 3600
        blocks.append({"burst": b, "name": spec["name"], "start_h": (s - t0) // 3600,
                       "hours": int(hours), "jobs": int(len(k)),
                       "rho_cpu": k["cpu_s"].sum() / (CAP_CPU * (e - s)),
                       "rho_mem": k["mem_s"].sum() / (CAP_MEM * (e - s))})
    return parts, blocks


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
    burst_parts, bursts = place_bursts(pool, cfg, week_h, 0, seed, name, target, burst_account,
                                       rng)
    parts += burst_parts

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


def segment_days(pool: pd.DataFrame, spec: dict, day0: int, n_days: int, seed: int,
                 name: str, part: int, target: float, burst_account: str, label: str
                 ) -> pd.DataFrame:
    """One pattern over n_days whole days from day0, thinned to target over the whole window.

    Each pool job is given a seeded day in the window and keeps its time of day. Thinning the
    window as one, not day by day, halves the step one big job makes in its load over two days
    (scenario D).
    """
    day = rng_for(seed, name, part + 50).integers(0, n_days, len(pool))
    cand = pool.assign(submit_s=(day0 + day) * DAY_S + pool["tod"])
    window = n_days * DAY_S
    if "burst_share" in spec:
        # The burst account supplies `share` of the target; other accounts fill the rest.
        a = cand[cand["account"] == burst_account]
        o = cand[cand["account"] != burst_account]
        ka, _ = thin(a, rng_for(seed, name, part), target * spec["burst_share"], window)
        ko, _ = thin(o, rng_for(seed, name, part + 100), target, window,
                     base_cpu_s=ka["cpu_s"].sum(), base_mem_s=ka["mem_s"].sum())
        kept = pd.concat([ka, ko])
    else:
        kept, _ = thin(segment_candidates(cand, spec), rng_for(seed, name, part), target, window)
    return kept.assign(segment=label)


def warm_start_jobs(pool: pd.DataFrame, seed: int, name: str, target: float) -> pd.DataFrame:
    """Jobs already running when a workload starts, as in a busy cluster (SPEC §9.4).

    At a random instant of steady operation, a job is running with probability proportional
    to its run time, and it has used a uniform share of that run time. So pool jobs are drawn
    in a seeded order weighted by run time (without replacement: exponential keys / run
    time), each keeps a seeded uniform share of its run time (at least 1 s) and its own time
    limit, and the set is the prefix whose allocated CPUs or memory, whichever is the larger
    share of the cluster, is closest to target. The caller submits them all at time 0.
    """
    r = pool["runtime_s"].to_numpy(float)
    key = rng_for(seed, name, 1).exponential(size=len(pool)) / np.maximum(r, 1.0)
    c = pool.iloc[np.argsort(key, kind="stable")]
    occ = np.maximum(np.cumsum(c["cpus"].to_numpy()) / CAP_CPU,
                     np.cumsum(c["mem_mb"].to_numpy()) / CAP_MEM)
    k = int(np.searchsorted(occ, target))
    if k >= len(occ):
        n = len(occ)
    else:
        n = k if k > 0 and abs(occ[k - 1] - target) < abs(occ[k] - target) else k + 1
    kept = c.iloc[:n]
    share = rng_for(seed, name, 2).random(n)
    left = np.maximum(1, np.ceil(share * kept["runtime_s"].to_numpy())).astype("int64")
    return kept.assign(runtime_s=left, cpu_s=kept["cpus"].to_numpy() * left,
                       mem_s=kept["mem_mb"].to_numpy() * left, segment="warmstart")


def day_workload(jobs: pd.DataFrame, scenario: str, pattern: str, cfg: dict, seed: int,
                 target: float) -> tuple[pd.DataFrame, dict]:
    """Scenario D (SPEC §9.4): natural lead-in, measured days of one pattern, natural lead-out.

    Every job keeps its time of day. With L = leadin_hours and M = measured_days, the workload
    holds: the lead-in [0, L), natural jobs from the last L hours of a day; the measured days
    [L, L + M days), which start at midnight; the lead-out, natural jobs for leadout_hours from
    the next midnight. Lead-in and lead-out are thinned to target and depend on the seed only,
    so all patterns of one seed share them. With warm_start, the running set of a busy cluster
    is submitted at time 0 too (warm_start_jobs; seed only). Segments: 'warmstart', 'leadin',
    'leadout', and for the measured days the pattern key, or for an overlay pattern 'base' and
    'burstNN:<name>' as in Y.
    """
    pool = jobs[jobs["week"].isin(cfg["pool_weeks"])]
    burst_account = top_account(pool)
    lead = int(cfg["leadin_hours"] * 3600)
    n_days = int(cfg.get("measured_days", 1))
    tail = int(cfg.get("leadout_hours", 0) * 3600)
    name = f"{scenario}-{pattern}"
    spec = cfg["patterns"][pattern]

    # Built on a frame where the lead-in ends day 0 and the measured days are days 1..M, then
    # shifted so the workload starts at the lead-in.
    parts, bursts = [], []
    if cfg.get("warm_start"):
        warm = warm_start_jobs(pool, seed, f"{scenario}-warmstart", target)
        parts.append(warm.assign(submit_s=DAY_S - lead))
    if lead:
        parts.append(place_window(pool, DAY_S - lead, DAY_S, target, seed,
                                  f"{scenario}-leadin", 1, "leadin"))
    t0, t1 = DAY_S, (1 + n_days) * DAY_S
    if "overlay" in spec:
        ov = spec["overlay"]
        parts.append(place_window(pool, t0, t1, target * ov["base_share"], seed, name, 1,
                                  "base"))
        if "block_hours" in ov:          # a fixed schedule of blocks, patterns in turn
            bp, bursts = place_blocks(pool, ov, t0, t1, seed, name, target, burst_account)
        else:                            # bursts at seeded random times, as in Y
            bp, bursts = place_bursts(pool, ov, 24 * n_days, 24, seed, name, target,
                                      burst_account, rng_for(seed, name, 999_999))
        parts += bp
    else:
        parts.append(segment_days(pool, spec, 1, n_days, seed, name, 1000, target,
                                  burst_account, pattern))
    if tail:
        parts.append(place_window(pool, t1, t1 + tail, target, seed, f"{scenario}-leadout", 1,
                                  "leadout"))
    kept = pd.concat(parts)
    kept = kept.assign(submit_s=kept["submit_s"] - (DAY_S - lead))

    def load(k, window):
        return {"jobs": int(len(k)), "rho_cpu": k["cpu_s"].sum() / (CAP_CPU * window),
                "rho_mem": k["mem_s"].sum() / (CAP_MEM * window)}

    seg = kept["segment"]
    meas = kept[~seg.isin(["warmstart", "leadin", "leadout"])]
    warm = kept[seg == "warmstart"]
    lo = lead
    info = {"pattern": pattern, "pattern_name": spec["name"], "pool_weeks": cfg["pool_weeks"],
            "burst_account": burst_account, "leadin_hours": cfg["leadin_hours"],
            "measured_days": n_days, "leadout_hours": cfg.get("leadout_hours", 0),
            "measured_window_s": [lo, lo + n_days * DAY_S],
            "start_time_of_day_utc": f"{(DAY_S - lead) % DAY_S // 3600:02d}:00",
            "warmstart": ({"jobs": int(len(warm)), "cpu_occupancy": warm["cpus"].sum() / CAP_CPU,
                           "mem_occupancy": warm["mem_mb"].sum() / CAP_MEM}
                          if len(warm) else None),
            "leadin": load(kept[seg == "leadin"], lead) if lead else None,
            "measured": load(meas, n_days * DAY_S),
            "measured_by_day": [
                {"day": d, **load(meas[(meas["submit_s"] - lo) // DAY_S == d], DAY_S)}
                for d in range(n_days)],
            "leadout": load(kept[seg == "leadout"], tail) if tail else None}
    if bursts:
        info["bursts"] = bursts
    return kept, info


# --------------------------------------------------------------------------- output

def iso(ts: int) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


def finalize(kept: pd.DataFrame) -> pd.DataFrame:
    """Order by submission, number from 1001, and keep the SPEC §3.3 fields."""
    out = kept.sort_values(["submit_s", "src_jobid"], kind="stable").reset_index(drop=True)
    out.insert(0, "job_id", np.arange(1001, 1001 + len(out)))
    out.insert(1, "name", "jobid_" + out["job_id"].astype(str))
    out["timelimit_min"] = -(-out["timelimit_s"] // 60)
    size_qos = yaml.safe_load(WEIGHTS.read_text())["size_qos"]
    out["qos"] = out["class"].astype(str).str.split("-").str[0].map(size_qos)
    cols = ["job_id", "name", "submit_s", "runtime_s", "timelimit_min", "cpus", "mem_mb",
            "user", "account", "class", "qos", "src_jobid", "src_state", "segment"]
    out = out[cols].copy()
    for c in ["user", "account", "src_state"]:
        out[c] = out[c].astype(str)
    out["class"] = pd.Categorical(out["class"].astype(str), categories=load_config()["classes"],
                                  ordered=True)
    return out


def event_line(r) -> str:
    return (f"-dt {r.submit_s} -e submit_batch_job | -J {r.name} -sim-walltime {r.runtime_s} "
            f"--uid={r.user} -t {r.timelimit_min} -n {r.cpus} --ntasks-per-node={r.cpus} -N 1 "
            f"--mem={r.mem_mb} -A {r.account} -p normal -q {r.qos} pseudo.job")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                       text=True)
    return r.stdout.strip()


def write_scenario(df: pd.DataFrame, name: str, seed: int, target: float, info: dict,
                   quota: int | None, window_s: int = trace.WEEK_S) -> dict:
    d = TRACES / f"{name}-s{seed}"
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / "jobs.parquet", index=False)
    (d / "events.txt").write_text("\n".join(event_line(r) for r in df.itertuples()) + "\n")
    pairs = sorted(set(zip(df["account"], df["user"])))
    (d / "accounts.txt").write_text("# account\tuser\n" + "".join(f"{a}\t{u}\n" for a, u in pairs))

    meas = df[~df["segment"].isin(["warmstart", "leadin", "leadout"])]   # measured window only
    cpu_s = float((meas["cpus"] * meas["runtime_s"]).sum())
    mem_s = float((meas["mem_mb"] * meas["runtime_s"]).sum())
    manifest = {
        "scenario": name, "seed": seed, "built_from_commit": git_commit(),
        "filters": {"partitions": list(trace.KEEP_PARTITIONS), "gpus": 0, "started": 1,
                    "single_node": True, "max_cpus": trace.NODE_CORES,
                    "max_mem_mb": trace.NODE_MEM_MB, "max_timelimit_s": trace.MAX_TIMELIMIT_S},
        "target_rho": target, "class_quota": quota,
        "achieved": {"rho_cpu": cpu_s / (CAP_CPU * window_s),
                     "rho_mem": mem_s / (CAP_MEM * window_s)},
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
    days = {f"{s}-{p}": (s, p) for s, c in cfg.get("day", {}).items() for p in c["patterns"]}
    all_names = (list(cfg["natural"]) + list(cfg["shifting"]) + list(cfg.get("overlay", {}))
                 + list(days))
    names = names or all_names
    # A day scenario's own name (D) stands for all its patterns.
    names = [n for x in names for n in ([k for k in days if days[k][0] == x] or [x])]
    unknown = [n for n in names if n not in all_names]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {all_names}")
    jobs = load_jobs()
    manifests = []
    for name in names:
        for seed in seeds:
            window = trace.WEEK_S
            if name in cfg["natural"]:
                kept, info = natural_week(jobs, name, cfg["natural"][name]["week"], seed, target,
                                          quota)
            elif name in cfg["shifting"]:
                kept, info = shifting_week(jobs, name, cfg["shifting"][name], seed, target)
            elif name in days:
                s, pat = days[name]
                kept, info = day_workload(jobs, s, pat, cfg["day"][s], seed, target)
                window = info["measured_days"] * DAY_S
            else:
                kept, info = overlay_week(jobs, name, cfg["overlay"][name], seed, target)
            m = write_scenario(finalize(kept), name, seed, target, info, quota, window)
            manifests.append(m)
            a = m["achieved"]
            print(f"{name}-s{seed}: {m['jobs']:,} jobs, {m['accounts']} accounts, "
                  f"rho_cpu={a['rho_cpu']:.3f} rho_mem={a['rho_mem']:.3f}")
    write_summary(manifests)
    return manifests


def merge_csv(path: Path, rows: list[dict], keys: list[str]) -> None:
    """Write rows to path, keeping the old rows of (scenario, seed) pairs not rebuilt now."""
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path)
        built = set(zip(new["scenario"], new["seed"]))
        old = old[[(s, d) not in built for s, d in zip(old["scenario"], old["seed"])]]
        # Columns only the replaced rows had (an older layout) go with them.
        old = old.loc[:, old.notna().any() | old.columns.isin(new.columns)]
        new = pd.concat([old, new.dropna(axis=1, how="all")], ignore_index=True)
    new.sort_values(keys).to_csv(path, index=False, float_format="%.4f")


def write_summary(manifests: list[dict]) -> None:
    """summary.csv for every scenario; X_segments, Y_bursts, D_days and D_bursts by family."""
    SUMMARY.mkdir(parents=True, exist_ok=True)
    rows, segs, bursts, days = [], [], {}, []
    for m in manifests:
        row = {"scenario": m["scenario"], "seed": m["seed"], "jobs": m["jobs"],
               "users": m["users"], "accounts": m["accounts"],
               "rho_cpu": m["achieved"]["rho_cpu"], "rho_mem": m["achieved"]["rho_mem"],
               "p": m.get("p")}
        row.update(m["class_counts"])
        rows.append(row)
        family = m["scenario"].split("-")[0]
        for s in m.get("segments", []):
            segs.append({"scenario": m["scenario"], "seed": m["seed"], **s})
        for b in m.get("bursts", []):
            bursts.setdefault(family, []).append({"scenario": m["scenario"], "seed": m["seed"], **b})
        if "measured" in m:
            r = {"scenario": m["scenario"], "seed": m["seed"], "pattern": m["pattern"]}
            for part in ("warmstart", "leadin", "measured", "leadout"):
                for k, v in (m[part] or {}).items():
                    r[f"{part}_{k}"] = v
            for d in m["measured_by_day"]:
                for k in ("jobs", "rho_cpu", "rho_mem"):
                    r[f"day{d['day']}_{k}"] = d[k]
            days.append(r)
    merge_csv(SUMMARY / "summary.csv", rows, ["scenario", "seed"])
    if segs:
        merge_csv(SUMMARY / "X_segments.csv", segs, ["scenario", "seed", "segment"])
    for family, b in bursts.items():
        merge_csv(SUMMARY / f"{family}_bursts.csv", b, ["scenario", "seed", "burst"])
    if days:
        merge_csv(SUMMARY / "D_days.csv", days, ["scenario", "seed"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenarios", nargs="*")
    p.add_argument("--seeds", nargs="*", type=int)
    a = p.parse_args()
    build(a.scenarios or None, a.seeds or None)


if __name__ == "__main__":
    main()
