"""Basic statistics of a finished run from its sacct output (out/slurm_acct.out).

Usage: .venv/bin/python -m sim.runstats RUN_DIR [--step 3600]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from sim.check_run import mem_mb

ROOT = Path(__file__).resolve().parents[1]


def load_acct(run_dir: Path) -> pd.DataFrame:
    a = pd.read_csv(ROOT / run_dir / "out" / "slurm_acct.out", sep="|")
    a = a[a["JobName"].str.startswith("jobid_", na=False)].copy()
    for c in ["Submit", "Start", "End"]:
        a[c] = pd.to_datetime(a[c]).astype("int64") // 10**9
    a["mem_mb"] = a["ReqMem"].map(mem_mb)
    a["wait_s"] = a["Start"] - a["Submit"]
    return a


def timeline(a: pd.DataFrame, step: int = 3600) -> pd.DataFrame:
    """Pending jobs and allocated CPU/memory fractions sampled every `step` seconds."""
    t0 = a["Submit"].min()
    ts = np.arange(t0, a["End"].max() + step, step)
    sub, st, en = (np.sort(a[c].to_numpy()) for c in ["Submit", "Start", "End"])
    pending = np.searchsorted(sub, ts, "right") - np.searchsorted(st, ts, "right")
    run_cpu = np.zeros(len(ts))
    run_mem = np.zeros(len(ts))
    for col, out in [("NCPUS", run_cpu), ("mem_mb", run_mem)]:
        # cumulative sums of allocations starting/ending before each sample time
        o = np.argsort(a["Start"].to_numpy())
        cs = np.concatenate([[0], np.cumsum(a[col].to_numpy()[o])])
        out += cs[np.searchsorted(a["Start"].to_numpy()[o], ts, "right")]
        o = np.argsort(a["End"].to_numpy())
        ce = np.concatenate([[0], np.cumsum(a[col].to_numpy()[o])])
        out -= ce[np.searchsorted(a["End"].to_numpy()[o], ts, "right")]
    return pd.DataFrame({"hour": (ts - t0) / 3600, "pending": pending,
                         "cpu_frac": run_cpu / 512, "mem_frac": run_mem / 2_048_000})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--step", type=int, default=3600)
    a = p.parse_args()
    acct = load_acct(a.run_dir)
    tl = timeline(acct, a.step)
    print(f"jobs={len(acct)}  mean wait={acct['wait_s'].mean() / 60:.1f} min  "
          f"p95 wait={acct['wait_s'].quantile(.95) / 3600:.1f} h  "
          f"makespan={(acct['End'].max() - acct['Submit'].min()) / 3600:.1f} h")
    print(tl.iloc[::6].to_string(index=False, float_format=lambda v: f"{v:.2f}"))


if __name__ == "__main__":
    main()
