#!/usr/bin/env python3
"""Filter the source trace to single-node CPU jobs and cache the result (SPEC §9.2).

Filters, in order:
  1. keep partitions htc, htc_highmem and hpc
  2. drop jobs that never started (started=0 or no start time), then jobs with zero elapsed time
  3. drop jobs that don't fit one node: more than 32 cores, more than 128,000 MB, or multi-node
Each kept job's runtime is capped at its time limit (1.66% of kept jobs ran a few seconds past it).

Output:
  <out>                 parquet, one row per kept job, sorted by submit then jobid. Columns: jobid,
                        state, account, user, partition, submit, timelimit, runtime (capped),
                        cores, mem_mb
  <out>.counts.json     job and core-hour counts at every stage, as checked by scripts/check_b1.sh

Usage: python -I workloads/convert/filter_trace.py [--dataset DIR] [--out FILE]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from workloads.convert import common as T  # noqa: E402

READ_COLS = ["jobid", "state", "account", "user", "partition", "submit", "start", "timelimit",
             "elapsed", "allocmem", "alloccpus", "started", "hostnames"]
OUT_COLS = ["jobid", "state", "account", "user", "partition", "submit", "timelimit", "runtime",
            "cores", "mem_mb"]


def read_month(path):
    tb = pc.read_csv(path, parse_options=pc.ParseOptions(delimiter="\t"),
                     convert_options=pc.ConvertOptions(
                         null_values=["NULL"], strings_can_be_null=True, include_columns=READ_COLS,
                         column_types={"jobid": pa.string(), "hostnames": pa.string()}))
    return tb.to_pandas()


def core_hours(df, elapsed="elapsed"):
    return float((df.alloccpus.astype("float64") * df[elapsed].astype("float64")).sum() / 3600)


def filter_month(df):
    """Apply the filters to one month. Returns (kept frame, counts dict)."""
    c = {"source": len(df)}
    df = df[df.partition.isin(T.KEEP_PARTITIONS)]
    c["after_partition"] = len(df)

    never = (df.started.fillna(0) == 0) | df.start.isna()
    c["never_started"] = int(never.sum())
    df = df[~never]
    zero = df.elapsed.fillna(0) <= 0
    c["zero_elapsed"] = int(zero.sum())
    df = df[~zero]
    c["after_started"] = len(df)
    c["after_started_core_hours"] = core_hours(df)

    multinode = df.hostnames.fillna("").str.contains(r"[\[,]", regex=True)
    too_cores = df.alloccpus > T.NODE_CORES
    too_mem = df.allocmem > T.NODE_MEM_MB
    big = multinode | too_cores | too_mem
    c["too_big"] = int(big.sum())
    c["too_big_core_hours"] = core_hours(df[big])
    c["too_big_cores"] = int(too_cores.sum())
    c["too_big_mem"] = int(too_mem.sum())
    c["too_big_multinode"] = int(multinode.sum())
    df = df[~big]
    c["kept"] = len(df)
    c["kept_core_hours"] = core_hours(df)

    over = df.elapsed > df.timelimit
    c["capped"] = int(over.sum())
    c["capped_timeout"] = int((over & (df.state == "timeout")).sum())
    c["capped_over_s"] = (df.elapsed - df.timelimit)[over].to_numpy()
    out = pd.DataFrame({
        "jobid": df.jobid, "state": df.state, "account": df.account, "user": df.user,
        "partition": df.partition, "submit": df.submit.astype("int64"),
        "timelimit": df.timelimit.astype("int64"),
        "runtime": np.minimum(df.elapsed, df.timelimit).astype("int64"),
        "cores": df.alloccpus.astype("int64"), "mem_mb": df.allocmem.astype("int64")})
    return out, c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(T.DEFAULT_DATASET), help="folder holding 01.tsv-06.tsv")
    ap.add_argument("--out", default=str(T.DEFAULT_CACHE))
    a = ap.parse_args()

    frames, per_month = [], {}
    for m in T.MONTHS:
        path = Path(a.dataset) / f"{m:02d}.tsv"
        kept, c = filter_month(read_month(path))
        frames.append(kept)
        per_month[f"{m:02d}"] = c
        print(f"{path.name}: {c['source']:,} jobs, {c['kept']:,} kept", flush=True)

    df = pd.concat(frames, ignore_index=True)
    del frames
    dup = int(df.jobid.duplicated().sum())
    df = df.sort_values(["submit", "jobid"], kind="stable").reset_index(drop=True)
    for col in ("state", "account", "user", "partition"):
        df[col] = df[col].astype("category")

    tot = {k: sum(c[k] for c in per_month.values()) for k in per_month["01"] if k != "capped_over_s"}
    over_s = np.concatenate([c["capped_over_s"] for c in per_month.values()])
    for c in per_month.values():
        del c["capped_over_s"]
    counts = {
        "source_jobs": tot["source"],
        "dropped_partition": tot["source"] - tot["after_partition"],
        "dropped_never_started": tot["never_started"],
        "dropped_zero_elapsed": tot["zero_elapsed"],
        "dropped_too_big": tot["too_big"],
        "too_big_job_frac": tot["too_big"] / tot["after_started"],
        "too_big_core_hour_frac": tot["too_big_core_hours"] / tot["after_started_core_hours"],
        "too_big_by_reason": {"cores": tot["too_big_cores"], "mem": tot["too_big_mem"],
                              "multinode": tot["too_big_multinode"]},
        "kept_jobs": tot["kept"],
        "kept_core_hours": tot["kept_core_hours"],
        "duplicate_jobids": dup,
        "capped_jobs": tot["capped"],
        "capped_frac": tot["capped"] / tot["kept"],
        "capped_timeout_frac": tot["capped_timeout"] / max(1, tot["capped"]),
        "capped_median_over_s": float(np.median(over_s)) if len(over_s) else 0.0,
        "users": int(df.user.nunique()),
        "accounts": int(df.account.nunique()),
        "submit_min": int(df.submit.min()), "submit_max": int(df.submit.max()),
        "max_timelimit_s": int(df.timelimit.max()),
        "min_mem_mb": int(df.mem_mb.min()),
        "per_month": per_month,
    }

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pandas(df[OUT_COLS], preserve_index=False), tmp,
                   row_group_size=1_000_000, compression="zstd")
    tmp.replace(out)
    Path(str(out) + ".counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in counts.items() if k != "per_month"}, indent=2))


if __name__ == "__main__":
    main()
