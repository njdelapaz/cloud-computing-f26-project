"""Parse Slurm's DebugFlags=Priority output from slurmctld.log (the simulator's sprio).

Each priority calculation for a job logs, in order (Slurm 23.11 priority/multifactor):
  Weighted Age|Assoc|Fairshare|JobSize|Partition|QOS priority is <factor> * <weight> = <value>
  Site priority is <n>
  Weighted TRES:<name> is <factor> * <weight> = <value>     (only non-zero TRES)
  Job <id> priority: ... = <total>
Timestamps are simulated time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

TS = re.compile(r"^\[(\S+)\]")
WEIGHTED = re.compile(r"_get_priority_internal: Weighted (\w+) priority is (\S+) \* (\S+) = (\S+)")
TRES = re.compile(r"_get_priority_internal: Weighted TRES:(\w+) is (\S+) \* (\S+) = (\S+)")
JOB = re.compile(r"_get_priority_internal: Job (\d+) priority: .* = (\S+)$")
FACTORS = ["Age", "QOS", "Fairshare", "JobSize", "TRES_cpu", "TRES_mem"]


def parse(path: Path, every_s: int = 3600, start: str | None = None) -> pd.DataFrame:
    """Per-job factors, weights and totals at priority calculations.

    Keeps only calculations logged in the first minute after each multiple of every_s seconds
    of simulated time (one snapshot of the queue per interval). Columns:
    t, job_id, <factor>_f, <factor>_w, <factor>_v for FACTORS, and priority.
    """
    rows, cur, keep = [], {}, False
    t0 = pd.Timestamp(start) if start else None
    with open(path, errors="replace") as fh:
        for line in fh:
            if "_get_priority_internal" not in line:
                continue
            m = WEIGHTED.search(line)
            if m:
                name = m.group(1)
                if name == "Age":                      # first line of a job's block
                    ts = pd.Timestamp(TS.match(line).group(1))
                    t0 = t0 or ts.floor("D")
                    secs = int((ts - t0).total_seconds())
                    keep = secs % every_s < 60
                    cur = {"t": ts}
                if keep and name in ("Age", "Fairshare", "JobSize", "QOS"):
                    f, w, v = (float(x) for x in m.groups()[1:])
                    cur.update({f"{name}_f": f, f"{name}_w": w, f"{name}_v": v})
                continue
            if not keep:
                continue
            m = TRES.search(line)
            if m:
                key = f"TRES_{m.group(1).lower()}"
                f, w, v = (float(x) for x in m.groups()[1:])
                cur.update({f"{key}_f": f, f"{key}_w": w, f"{key}_v": v})
                continue
            m = JOB.search(line)
            if m and cur:
                cur["job_id"] = int(m.group(1))
                cur["priority"] = float(m.group(2))
                rows.append(cur)
                cur = {}
    df = pd.DataFrame(rows)
    for fac in FACTORS:
        for s in ("_f", "_w", "_v"):
            if fac + s not in df:
                df[fac + s] = 0.0
    if len(df):
        df[[c for c in df if c.endswith(("_f", "_w", "_v"))]] = \
            df[[c for c in df if c.endswith(("_f", "_w", "_v"))]].fillna(0.0)
        # One row per job per snapshot: keep the last calculation in the snapshot minute.
        df["snapshot"] = df["t"].dt.floor(f"{max(every_s, 60)}s")
        df = df.drop_duplicates(["snapshot", "job_id"], keep="last").reset_index(drop=True)
    return df
