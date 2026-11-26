"""Read the IN2P3 2024 trace in dataset/, cache it as Parquet, and apply SPEC §3.2 filters.

dataset/ is read-only. The cache lives in workloads/cache/ (gitignored) and is rebuilt from
dataset/ whenever a TSV is newer than its Parquet file.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset"
CACHE = ROOT / "workloads" / "cache"

# Simulated cluster (SPEC §2.1).
NODE_CORES = 32
NODE_MEM_MB = 128_000
NODES = 16
CLUSTER_CORES = NODES * NODE_CORES          # 512
CLUSTER_MEM_MB = NODES * NODE_MEM_MB        # 2,048,000
MAX_TIMELIMIT_S = 7 * 86_400                # partition MaxTime

KEEP_PARTITIONS = ("htc", "hpc", "htc_highmem", "flash")

# 2024-01-01 00:00 UTC, a Monday. Week w covers [WEEK0 + w*7d, WEEK0 + (w+1)*7d).
WEEK0 = 1_704_067_200
WEEK_S = 7 * 86_400

SCHEMA = {
    "jobid": pa.int64(),
    "state": pa.string(),
    "account": pa.string(),
    "user": pa.string(),
    "partition": pa.string(),
    "submit": pa.int64(),
    "eligible": pa.int64(),
    "start": pa.int64(),
    "end": pa.int64(),
    "timelimit": pa.int64(),
    "elapsed": pa.int64(),
    "allocmem": pa.int64(),
    "maxrss": pa.int64(),
    "alloccpus": pa.int32(),
    "totalcpu": pa.int64(),
    "allocgpus": pa.int32(),
    "started": pa.int8(),
    "maxdiskwrite": pa.int64(),
    "nsteps": pa.int32(),
    "el9": pa.int8(),
    "hostnames": pa.string(),
}


def months() -> list[str]:
    """Month ids present in dataset/, e.g. ['01', ..., '06']."""
    return sorted(p.stem for p in DATASET.glob("*.tsv"))


def cache_month(month: str, force: bool = False) -> Path:
    """Convert dataset/<month>.tsv to workloads/cache/<month>.parquet if needed."""
    src = DATASET / f"{month}.tsv"
    dst = CACHE / f"{month}.parquet"
    if not force and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    CACHE.mkdir(parents=True, exist_ok=True)
    table = pacsv.read_csv(
        src,
        parse_options=pacsv.ParseOptions(delimiter="\t"),
        convert_options=pacsv.ConvertOptions(column_types=SCHEMA, null_values=["NULL"],
                                              strings_can_be_null=True),
    )
    hosts = pa.compute.utf8_trim_whitespace(table["hostnames"])
    table = table.set_column(table.schema.get_field_index("hostnames"), "hostnames", hosts)
    tmp = dst.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(dst)
    return dst


def load(columns: list[str] | None = None, month_ids: list[str] | None = None) -> pd.DataFrame:
    """Load the cached trace (building the cache as needed) for the given months."""
    month_ids = month_ids or months()
    frames = [pq.read_table(cache_month(m), columns=columns).to_pandas() for m in month_ids]
    return pd.concat(frames, ignore_index=True)


def filter_mask(df: pd.DataFrame) -> pd.Series:
    """SPEC §3.2: CPU-only batch partitions, ran, fits one node, limit within MaxTime."""
    single_host = ~df["hostnames"].fillna("").str.contains(",", regex=False)
    return (
        df["partition"].isin(KEEP_PARTITIONS)
        & (df["allocgpus"] == 0)
        & (df["started"] == 1)
        & df["allocmem"].notna()
        & single_host
        & (df["alloccpus"] >= 1)
        & (df["alloccpus"] <= NODE_CORES)
        & (df["allocmem"] <= NODE_MEM_MB)
        & (df["timelimit"] >= 1)
        & (df["timelimit"] <= MAX_TIMELIMIT_S)
    )


def runtime_s(df: pd.DataFrame) -> pd.Series:
    """SPEC §3.3: max(1, min(elapsed, timelimit))."""
    return np.maximum(1, np.minimum(df["elapsed"], df["timelimit"])).astype("int64")


def week_of(ts: pd.Series) -> pd.Series:
    """Week index counted from Monday 2024-01-01 00:00 UTC (negative before it)."""
    return np.floor_divide(ts - WEEK0, WEEK_S).astype("int64")
