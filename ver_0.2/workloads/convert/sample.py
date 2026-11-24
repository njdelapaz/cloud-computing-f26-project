#!/usr/bin/env python3
"""Random-job sampler: one window of the filtered trace, one seed -> jobs.csv + meta.json (SPEC §9.2, §13.2).

The trace covers [window_start, window_end) of submit times:
  window_start = START - WARMUP      warm-up: fills fair-share history, excluded from metrics
  warmup_end   = START
  window_end   = START + LENGTH      the measured window
Each job in that span gets a random key from the seed and is kept when its key is below p. p is
set per seed so the measured window's offered load, sum(cores x runtime) / (512 cores x LENGTH),
reaches TARGET (default 0.85), landing within +2% of it; the warm-up uses the same p (details in
sample()). LENGTH and WARMUP default to 1 day each until B3/P4 settle them (SPEC §11).

Usage:
  python -I workloads/convert/sample.py --start 2024-01-15 [--length 1d] [--warmup 1d] [--seed 1]
         [--load 0.85] [--cache FILE] [--out DIR]
Writes DIR/jobs.csv and DIR/meta.json (default DIR: workloads/traces/<start>_<length>_w<warmup>_s<seed>/)
and checks both with scripts/validate.py. With --convert it also writes the simulator input (to_sim.py).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from workloads.convert import common as T  # noqa: E402
import validate  # noqa: E402
from workloads.convert import to_sim  # noqa: E402

OVERSHOOT_TOL = 0.02     # a sample's offered load lands in [target, target x 1.02]
JOBS_COLS = ["job_id", "submit", "user", "account", "cores", "mem_mb", "time_limit_s", "runtime_s",
             "src_jobid", "src_partition", "src_state"]


def load_span(cache, t0, t1):
    """Filtered jobs submitted in [t0, t1), sorted by submit then jobid."""
    tb = pq.read_table(cache, filters=[("submit", ">=", t0), ("submit", "<", t1)])
    df = tb.to_pandas()
    for col in ("state", "account", "user", "partition"):
        df[col] = df[col].astype(str)
    return df.sort_values(["submit", "jobid"], kind="stable").reset_index(drop=True)


def offered_load(cores, runtime, length_s):
    return float(np.sum(np.asarray(cores, dtype="float64") * np.asarray(runtime, dtype="float64"))
                 / (T.CORES * length_s))


def sample(df, warmup_end, window_end, seed, target, tol=OVERSHOOT_TOL):
    """Keep each job whose seeded random key is below p, with p set so the measured window's
    offered load reaches the target.

    Every job in the span gets a key u ~ U(0,1) from the seed. Measured-window jobs are taken in
    increasing u until the window's offered load reaches `target`; p is the key of the job that
    reaches it. A job that would push the load more than `tol` above the target is skipped (rare:
    only a job worth a large share of the window can do it). Warm-up jobs are kept when u < p.
    Plain Bernoulli sampling at p = target / (window's full load) lands within ±5% of the target
    only about half the time on 1-day windows, because a few long, wide jobs dominate a day's
    core-hours; this lands in [target, target x (1 + tol)].

    Returns (sampled frame, p, offered load of the whole measured window before sampling,
    number of skipped jobs)."""
    length = window_end - warmup_end
    measured = (df.submit >= warmup_end).to_numpy()
    work = (df.cores.to_numpy(dtype="float64") * df.runtime.to_numpy(dtype="float64"))
    full_load = float(work[measured].sum() / (T.CORES * length))
    if full_load < target * (1 + tol):
        raise SystemExit(f"the whole window offers only {full_load:.3f} of 512 cores, "
                         f"below the target {target}; can't sample up")
    u = np.random.default_rng(seed).random(len(df))
    budget, cap = target * T.CORES * length, target * (1 + tol) * T.CORES * length
    idx = np.flatnonzero(measured)
    keep = np.zeros(len(df), dtype=bool)
    cum, p, skipped = 0.0, None, 0
    for i in idx[np.argsort(u[idx], kind="stable")]:
        if cum + work[i] > cap:
            skipped += 1
            continue
        keep[i] = True
        cum += work[i]
        if cum >= budget:
            p = float(u[i])
            break
    if p is None:
        raise SystemExit("couldn't reach the target load without overshooting; window too lumpy")
    keep |= ~measured & (u <= p)
    return df[keep].reset_index(drop=True), p, full_load, skipped


def to_jobs_frame(s):
    import pandas as pd
    return pd.DataFrame({
        "job_id": np.arange(1, len(s) + 1),
        "submit": s.submit.astype("int64"),
        "user": s.user, "account": s.account,
        "cores": s.cores.astype("int64"), "mem_mb": s.mem_mb.astype("int64"),
        "time_limit_s": s.timelimit.map(T.time_limit_minutes).astype("int64") * 60,
        "runtime_s": s.runtime.astype("int64"),
        "src_jobid": s.jobid, "src_partition": s.partition, "src_state": s.state,
    })[JOBS_COLS]


def run(start, length, warmup, seed, target, cache, out, allow_partial=False):
    window_start, warmup_end, window_end = start - warmup, start, start + length
    if not allow_partial and (window_start < T.COVERAGE_START or window_end > T.COVERAGE_END):
        raise SystemExit(f"window {T.iso(window_start)}..{T.iso(window_end)} is outside the span where the "
                         f"trace holds every job ({T.iso(T.COVERAGE_START)}..{T.iso(T.COVERAGE_END)}); "
                         f"pass --allow-partial to sample it anyway")
    df = load_span(cache, window_start, window_end)
    s, p, full_load, skipped = sample(df, warmup_end, window_end, seed, target)
    jobs = to_jobs_frame(s)
    meta = {"window_start": int(window_start), "window_end": int(window_end),
            "warmup_end": int(warmup_end), "seed": int(seed), "sampling_rate": float(p),
            "target_load": float(target), "jobs": int(len(jobs)),
            "users": int(jobs.user.nunique()), "accounts": int(jobs.account.nunique())}

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jobs.to_csv(out / "jobs.csv", index=False, lineterminator="\n")
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    errors = validate.validate_meta((out / "meta.json").read_text())
    errors += validate.validate_jobs((out / "jobs.csv").read_text(), meta)
    if errors:
        raise SystemExit("validation failed:\n  " + "\n  ".join(errors[:20]))

    m = jobs.submit >= warmup_end
    stats = {"out": str(out), "window": f"{T.iso(window_start)}..{T.iso(window_end)}",
             "warmup_end": T.iso(warmup_end), "seed": seed, "sampling_rate": round(p, 5),
             "jobs": meta["jobs"], "measured_jobs": int(m.sum()),
             "users": meta["users"], "accounts": meta["accounts"],
             "unsampled_load": round(full_load, 3), "skipped": skipped,
             "offered_load": round(offered_load(jobs.cores[m], jobs.runtime_s[m], length), 4),
             "warmup_offered_load": round(offered_load(jobs.cores[~m], jobs.runtime_s[~m], warmup), 4)
             if warmup else None}
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="start of the measured window, UTC (e.g. 2024-01-15)")
    ap.add_argument("--length", default="1d", help="measured window length (default 1d; set in B3/P4)")
    ap.add_argument("--warmup", default="1d", help="warm-up before the window (default 1d; set in B3/P4)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--load", type=float, default=0.85, help="target offered load on 512 cores")
    ap.add_argument("--cache", default=str(T.DEFAULT_CACHE), help="filtered trace from filter_trace.py")
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--allow-partial", action="store_true",
                    help="allow windows near the trace's ends, where long jobs are missing")
    ap.add_argument("--convert", action="store_true",
                    help="also write the simulator input (to_sim.py) into the output folder")
    a = ap.parse_args()

    start, length, warmup = T.parse_time(a.start), T.parse_duration(a.length), T.parse_duration(a.warmup)
    if length <= 0 or warmup < 0:
        ap.error("need --length > 0 and --warmup >= 0")
    if warmup == 0:
        ap.error("--warmup must be > 0: SPEC §13.2 needs window_start <= warmup_end < window_end "
                 "and B2 excludes the warm-up from metrics")
    out = a.out or T.TRACES / f"{T.iso(start)[:10]}_{a.length}_w{a.warmup}_s{a.seed}"
    stats = run(start, length, warmup, a.seed, a.load, a.cache, out, a.allow_partial)
    if a.convert:
        stats["sim"] = to_sim.convert(out)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
