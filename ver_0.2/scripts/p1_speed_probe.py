#!/usr/bin/env python3
"""P1 speed probe: turn one window of the real trace into simulator input for the project cluster.

This is a quick, single-purpose converter, used only to measure how fast the simulator runs on
realistic load. B1 builds the real filter/sampler/converter (SPEC §9.2).

Output folder (default workloads/traces/p1_probe/):
  jobs.events      simulator events file
  users.sim        user name -> uid mapping the simulator needs
  sacctmgr.script  accounts and users, equal fair-share per account
  meta.json        window, sampling rate, job counts

Usage: .venv/bin/python -I scripts/p1_speed_probe.py --start 2024-01-15 --days 1 [--seed 1] [--load 0.85]
"""
import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pc

VER02 = Path(__file__).resolve().parent.parent
CORES, NODE_CORES, NODE_MEM_MB = 512, 32, 128000
KEEP_PARTITIONS = {"htc", "htc_highmem", "hpc"}
COLS = ["account", "user", "partition", "submit", "start", "timelimit", "elapsed",
        "allocmem", "alloccpus", "hostnames"]


def load_months(months):
    frames = []
    for m in months:
        path = VER02 / "dataset" / f"{m:02d}.tsv"
        tb = pc.read_csv(path, parse_options=pc.ParseOptions(delimiter="\t"),
                         convert_options=pc.ConvertOptions(
                             null_values=["NULL"], strings_can_be_null=True, include_columns=COLS,
                             column_types={"hostnames": pa.string()}))
        frames.append(tb.to_pandas())
    return pd.concat(frames, ignore_index=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="window start date (UTC), e.g. 2024-01-15")
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--load", type=float, default=0.85, help="target offered load on 512 cores")
    ap.add_argument("--out", default=str(VER02 / "workloads" / "traces" / "p1_probe"))
    a = ap.parse_args()

    t0 = int(datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp())
    t1 = t0 + int(a.days * 86400)
    # Files are split by job end month; jobs submitted in the window can end up to 7 days later
    first = datetime.fromtimestamp(t0, timezone.utc).month
    last = datetime.fromtimestamp(t1 + 7 * 86400, timezone.utc).month
    df = load_months(range(first, min(last, 6) + 1))

    multinode = df.hostnames.fillna("").str.contains(r"[\[,]")
    keep = (df.partition.isin(KEEP_PARTITIONS) & df.start.notna() & (df.elapsed > 0)
            & (df.alloccpus <= NODE_CORES) & (df.allocmem <= NODE_MEM_MB) & ~multinode
            & (df.submit >= t0) & (df.submit < t1))
    w = df[keep].copy()

    demand = float((w.alloccpus * w.elapsed).sum())
    p = min(1.0, a.load * CORES * (t1 - t0) / demand)
    rng = np.random.default_rng(a.seed)
    s = w[rng.random(len(w)) < p].sort_values("submit").reset_index(drop=True)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "jobs.events", "w") as f:
        for i, j in s.iterrows():
            minutes = max(1, math.ceil(j.timelimit / 60))
            f.write(f"-dt {int(j.submit - t0)} -e submit_batch_job | -J jobid_{i + 1} "
                    f"-sim-walltime {int(j.elapsed)} --uid={j.user} -t {minutes} "
                    f"-n {int(j.alloccpus)} --ntasks-per-node={int(j.alloccpus)} "
                    f"--mem={int(j.allocmem)} -A {j.account} -p normal -q normal pseudo.job\n")

    users = sorted(s.user.unique())
    with open(out / "users.sim", "w") as f:
        f.write("admin:1000:admin:1000\n")
        for k, u in enumerate(users):
            f.write(f"{u}:{2000 + k}:users:100\n")

    pairs = s.groupby("user").account.unique()
    accounts = sorted(s.account.unique())
    with open(out / "sacctmgr.script", "w") as f:
        f.write("modify QOS set normal Priority=0\n")
        f.write("add cluster Name=project Fairshare=1 QOS=normal\n")
        for acc in accounts:
            f.write(f"add account name={acc} Fairshare=100\n")
        f.write(f"add user name=admin DefaultAccount={accounts[0]} AdminLevel=Administrator\n")
        for u in users:
            accs = sorted(pairs[u])
            f.write(f"add user name={u} DefaultAccount={accs[0]}\n")
            for acc in accs[1:]:
                f.write(f"add user name={u} Account={acc}\n")

    meta = {"window_start_utc": a.start, "days": a.days, "time_start": t0, "seed": a.seed,
            "target_load": a.load, "sampling_rate": p, "jobs_in_window": int(len(w)),
            "jobs_sampled": int(len(s)), "users": len(users), "accounts": len(accounts),
            "offered_core_hours": float((s.alloccpus * s.elapsed).sum() / 3600)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
