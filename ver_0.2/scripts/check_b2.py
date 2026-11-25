#!/usr/bin/env python3
"""B2 gate (WORKFLOW B2): metrics on a tiny hand-computed trace match the hand calculation.

Usage: check_b2.py [--data DIR]
  DIR is a ver_0.2 checkout that holds the git-ignored real runs and traces (default: this one).

Gate checks (exit 1 if any fails):
  1. hand trace: `analysis/metrics.py` run as a command on tests/data/b2 prints every column of
     hand.json, with the value worked out in tests/data/b2/README.md.
  2. examples/: the decision- and placement-log metrics of examples/decisions.csv and
     examples/placements.csv match their hand values.
  3. absent logs: a run folder with only jobcomp.log gives empty overhead columns and the same
     job and node metrics.
Not gating (INFO): the metrics of three real virtual-clock runs, when DIR has them, written to
results/b2/real_runs.csv. With numpy, waits, utilization and node-load balance are recomputed
independently, second by second, which is exact because jobcomp times are whole seconds.
"""
import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02))
from analysis import metrics as M  # noqa: E402

DATA = VER02 / "tests" / "data" / "b2"
REAL = [("s0130_stock_1", "2024-01-30_1d_w1d_s2"), ("s0212_stock_1", "2024-02-12_1d_w1d_s3"),
        ("pr_stock_1", "p1_probe")]
SHOW = ["jobs_measured", "users_measured", "wait_mean_s", "wait_median_s", "wait_p95_s", "wait_max_s",
        "bsld_mean", "fair_jain_bsld", "fair_wait_spread", "fair_wait_worst_user_s", "fair_wait_best_user_s",
        "bal_load_sd", "bal_load_max", "util_cores", "util_mem", "stranded_core_h", "makespan_s",
        "throughput_per_day", "sched_n", "sched_us_mean", "sched_us_p95", "sched_us_max", "bf_n",
        "bf_us_mean", "bf_us_p95", "bf_us_max"]


def close(a, b):
    if isinstance(b, str) or a is None or b is None:
        return a == b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def compare(label, got, hand):
    rows = [(k, hand[k], got.get(k), close(got.get(k), hand[k])) for k in hand]
    extra = sorted(set(got) - set(hand))
    for k, h, g, ok in rows:
        print(f"  {k:<26} hand {fmt(h):>12}  computed {fmt(g):>12}  {'ok' if ok else 'MISMATCH'}")
    if extra:
        print(f"  columns without a hand value: {', '.join(extra)}")
    bad = sum(not ok for *_, ok in rows) + len(extra)
    print(f"{'PASS' if not bad else 'FAIL'}  {label}: {len(rows) - bad + len(extra)} of {len(rows)} match"
          + (f", {len(extra)} unchecked" if extra else ""))
    return not bad


def per_second_check(np, run_dir, meta_path, row):
    """Independent recomputation of a few metrics; returns (metric, metrics.py, independent) rows."""
    meta = M.load_meta(meta_path)
    w0, w1 = meta["warmup_end"], meta["window_end"]
    cluster = M.read_cluster(run_dir / "etc" / "slurm.conf")
    jobs = M.read_jobcomp(run_dir / "log" / "jobcomp.log")
    idx = {n: i for i, n in enumerate(cluster.names)}
    cores = np.zeros((len(idx), w1 - w0))
    mem = np.zeros_like(cores)
    for j in jobs:
        a, b = max(j.start, w0) - w0, min(j.end, w1) - w0
        if b > a:
            cores[idx[j.node], a:b] += j.cores
            mem[idx[j.node], a:b] += j.mem_mb
    cap_c, cap_m = np.array(cluster.cores)[:, None], np.array(cluster.mem_mb)[:, None]
    load = np.maximum(cores / cap_c, mem / cap_m)
    waits = np.array([j.start - j.submit for j in jobs if w0 <= j.submit < w1])
    # stranded cores: the state is constant between event times, so evaluate it once per interval
    submit, start = np.array([j.submit for j in jobs]), np.array([j.start for j in jobs])
    mpc = np.array([j.mem_mb / j.cores for j in jobs])
    times = sorted({t for j in jobs for t in (j.submit, j.start, j.end) if w0 < t < w1} | {w0, w1})
    stranded = 0.0
    for a, b in zip(times, times[1:]):
        waiting = (submit <= a) & (start > a)
        if waiting.any():
            free_c, free_m = cap_c[:, 0] - cores[:, a - w0], cap_m[:, 0] - mem[:, a - w0]
            med = np.median(mpc[waiting])
            stranded += free_c[(free_c > 0) & (free_m < med * free_c)].sum() * (b - a)
    got = {"util_cores": cores.sum() / (sum(cluster.cores) * (w1 - w0)),
           "util_mem": mem.sum() / (sum(cluster.mem_mb) * (w1 - w0)),
           "bal_load_sd": load.std(axis=0).mean(), "bal_load_max": load.max(axis=0).mean(),
           "stranded_core_h": stranded / 3600,
           "wait_mean_s": waits.mean(), "wait_median_s": np.median(waits),
           "wait_p95_s": np.percentile(waits, 95), "wait_max_s": waits.max()}
    return [(k, row[k], float(v)) for k, v in got.items()]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", type=Path, default=VER02, help="ver_0.2 checkout with the real runs and traces")
    a = p.parse_args()
    hand = json.loads((DATA / "hand.json").read_text())
    ok = True

    print("== 1. hand trace (tests/data/b2, derivation in its README.md)")
    out = subprocess.run([sys.executable, "-I", str(VER02 / "analysis" / "metrics.py"), str(DATA / "run"),
                          "--meta", str(DATA / "trace" / "meta.json"), "--label", "b2_tiny"],
                         capture_output=True, text=True)
    if out.returncode:
        print(out.stderr)
        print("FAIL  hand trace: analysis/metrics.py failed")
        ok = False
    else:
        ok &= compare("hand trace, every column", json.loads(out.stdout), hand["tiny"])

    print("\n== 2. examples/ decision and placement logs (no window)")
    dec, _ = M.decision_metrics(M.read_csv(VER02 / "examples" / "decisions.csv"))
    ok &= compare("examples/decisions.csv", dec, hand["examples_decisions"])
    pl = M.placement_metrics(M.read_csv(VER02 / "examples" / "placements.csv"))
    ok &= compare("examples/placements.csv", pl, hand["examples_placements"])

    print("\n== 3. absent logs give empty values")
    with tempfile.TemporaryDirectory() as d:
        run = Path(d) / "run"
        shutil.copytree(DATA / "run" / "etc", run / "etc")
        (run / "log").mkdir()
        shutil.copy(DATA / "run" / "log" / "jobcomp.log", run / "log")
        row = M.run_metrics(run, DATA / "trace" / "meta.json", label="b2_tiny")
    overhead = M.DECISION_KEYS + M.PLACEMENT_KEYS + M.CHURN_KEYS + M.COST_KEYS
    empty = [k for k in overhead if row[k] is None]
    same = [k for k in M.JOB_KEYS + M.NODE_KEYS if close(row[k], hand["tiny"][k])]
    good = len(empty) == len(overhead) and len(same) == len(M.JOB_KEYS + M.NODE_KEYS)
    print(f"{'PASS' if good else 'FAIL'}  only jobcomp.log: {len(empty)} of {len(overhead)} overhead columns empty, "
          f"{len(same)} of {len(M.JOB_KEYS + M.NODE_KEYS)} job and node metrics unchanged")
    ok &= good

    print("\n== INFO: real virtual-clock runs (not gating)")
    try:
        import numpy as np
    except ImportError:
        np = None
    rows = []
    for run_name, trace in REAL:
        run_dir = a.data / "results" / "vclock" / run_name
        meta = a.data / "workloads" / "traces" / trace / "meta.json"
        if not (run_dir / "log" / "jobcomp.log").exists() or not meta.exists():
            print(f"INFO  {run_name}: not present under {a.data}; skipped")
            continue
        row = M.run_metrics(run_dir, meta)
        rows.append(row)
        m = json.loads(meta.read_text())
        notes = [f"{row['jobs_total']} completion records"]
        if "jobs" in m:
            notes.append(f"meta.json says {m['jobs']} jobs")
        notes += [f"jobs_missing {fmt(row['jobs_missing'])}", f"trace_mismatches {fmt(row['trace_mismatches'])}",
                  f"runtimes from {row['runtime_source']}"]
        print(f"INFO  {run_name} ({trace}): " + ", ".join(notes))
        for k in SHOW:
            print(f"  {k:<26} {fmt(row[k])}")
        if np is not None:
            for k, mine, indep in per_second_check(np, run_dir, meta, row):
                print(f"  cross-check {k:<14} metrics.py {mine:.9g}  per-second {indep:.9g}  "
                      f"{'agree' if math.isclose(mine, indep, rel_tol=1e-9) else 'DIFFER'}")
    if rows:
        out_csv = VER02 / "results" / "b2" / "real_runs.csv"
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(M.KEYS)
            for r in rows:
                w.writerow(M.csv_value(r[k]) for k in M.KEYS)
        print(f"INFO  wrote {out_csv.relative_to(VER02)} ({len(rows)} rows)")
    if np is None:
        print("INFO  numpy not available: per-second cross-checks skipped")

    print(f"\nB2 gate: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
