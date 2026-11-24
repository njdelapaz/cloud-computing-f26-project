"""Shared constants and helpers for the B1 workload pipeline (SPEC §9).

Imported by filter_trace.py, sample.py and the episode finder. Only the standard library here, so
to_sim.py and the tests can import it without pandas.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

VER02 = Path(__file__).resolve().parents[2]
TRACES = VER02 / "workloads" / "traces"
DEFAULT_DATASET = VER02 / "dataset"
DEFAULT_CACHE = TRACES / "filtered.parquet"

# Simulated cluster (SPEC §9)
NODES, NODE_CORES, NODE_MEM_MB = 16, 32, 128_000
CORES = NODES * NODE_CORES

# Filter (SPEC §9.2)
KEEP_PARTITIONS = ("htc", "htc_highmem", "hpc")
MONTHS = range(1, 7)

# The source files hold jobs by *end* month, split at Paris local midnight. A submit time is fully
# covered only if every job submitted then ended inside the files' span: from 2024-01-01 00:00 Paris
# (2023-12-31 23:00 UTC) to 2024-07-01 00:00 Paris (2024-06-30 22:00 UTC), minus the longest time
# limit that survives the filter (7 days) at the end.
TRACE_FIRST_END = 1704063600          # 2023-12-31T23:00:00Z
TRACE_LAST_END = 1719784800           # 2024-06-30T22:00:00Z
MAX_TIME_LIMIT_S = 7 * 86400
COVERAGE_START = TRACE_FIRST_END
COVERAGE_END = TRACE_LAST_END - MAX_TIME_LIMIT_S   # 2024-06-23T22:00:00Z

# Calibration vs evaluation (SPEC §9.3, §11 #15), by UTC submit/onset time
EVAL_START = 1709251200               # 2024-03-01T00:00:00Z
EPISODE_START = 1704067200            # 2024-01-01T00:00:00Z
EPISODE_END = 1719792000              # 2024-07-01T00:00:00Z


def split_of(t):
    """'calibration' for January-February, 'evaluation' for March-June (UTC)."""
    return "calibration" if t < EVAL_START else "evaluation"


def parse_time(s):
    """'2024-03-05', '2024-03-05T12:00' or '2024-03-05T12:00:00' (UTC), or Unix seconds -> int."""
    if re.fullmatch(r"\d+", s):
        return int(s)
    dt = datetime.fromisoformat(s.rstrip("Z"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def iso(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(s):
    """'90s', '15min', '6h', '1d' or plain seconds -> int seconds."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|min|m|h|d)?", s.strip())
    if not m:
        raise ValueError(f"bad duration '{s}' (use e.g. 90s, 15min, 6h, 1d)")
    mult = {None: 1, "s": 1, "min": 60, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(round(float(m.group(1)) * mult))


def time_limit_minutes(timelimit_s):
    """Requested walltime rounded up to whole minutes, at least 1 (SPEC §13.2)."""
    return max(1, -(-int(timelimit_s) // 60))
