#!/usr/bin/env python3
"""Metrics for one simulator run (SPEC §10, §13.4), and their 95% confidence interval over seeds.
Standard library only.

Usage:
  metrics.py RUN_DIR --meta META.json [--jobs JOBS.csv] [--label NAME] [--json OUT] [--csv OUT]
      Prints one metrics row as JSON. --json also writes it to a file; --csv appends it to a CSV
      file, writing the header first when the file is new. --jobs defaults to jobs.csv next to
      META.json; without it, runtimes come from the completion records.
  metrics.py aggregate ROW.json ... [--json OUT]
      Mean and 95% CI half-width per metric over seed rows (SPEC §13.4: t = 2.776 for 5 seeds).
  metrics.py paired --a ROW.json ... --b ROW.json ... [--json OUT]
      Mean and 95% CI half-width of the per-seed differences A − B (SPEC §10, §13.4). The i-th row
      of --a and the i-th row of --b must be the same seed.

Inputs under RUN_DIR, where scripts/sim_run.sh writes them:
  log/jobcomp.log      completion records (required). Times are UTC, as the simulator image writes them
  etc/slurm.conf       node names, cores and memory (default: the project cluster, 16 × 32 cores × 128,000 MB)
  log/decisions.csv    decision log (SPEC §13.3)
  log/placements.csv   placement log (SPEC §13.3)
  log/queue_dumps.csv  pending order just before and after each ordering switch (format below)
  log/cost.csv         cost log (docker/patches/README.md)
The four logs are optional. Without one, its metrics are empty: None in JSON, blank in CSV.

Rules SPEC §13.3 and §13.4 adopted from B2's proposals (results/b2/summary.md):
  - Percentiles interpolate linearly between the two nearest ranks (numpy's default).
  - Decision, placement, queue-dump and cost rows count when their sim time is in
    [warmup_end, window_end), the window the time-averaged metrics use.
  - score_us statistics cover the node choices that were scored (score not empty). Stock placement
    logs score_us = 0 and is counted in placements_n only.
  - A job is pending from its submit time until its start. The stranded-core median is over every
    pending job, warm-up jobs included, and "below" is strict.
  - Wait spread is max(1, worst ÷ max(best, 10 s)) over the users' mean waits; the 10 s floor is
    bounded slowdown's τ.
  - Scheduler impact (SPEC §8 level 3): real_usec of the main-scheduler passes ("sched") and backfill
    cycles ("bf"), over the window and for the cycle just before (< t) and just after (>= t) each
    switch, averaged over the switches in the window.

Queue-dump format (log/queue_dumps.csv, SPEC §13.3), written only when a switch changes the ordering
weights: one row per pending job per dump,
  sim_time,phase,rank,job_id,priority
where sim_time is the switch's decision-log row, phase is "before" or "after" the priority
recompute, rank runs 1, 2, ... in the order the scheduler would consider the jobs, job_id is Slurm's
JobId and priority is the job's priority then. Churn for one switch is the share of pairs of jobs in
both dumps whose order flipped; switches with fewer than two such jobs have none.
"""
import argparse
import bisect
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict, namedtuple
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

TAU = 10                      # bounded-slowdown floor on runtime, seconds (SPEC §13.4)
PROJECT_NODES = [f"n{i:02d}" for i in range(1, 17)]
PROJECT_CORES, PROJECT_MEM_MB = 32, 128_000
# t quantile 0.975 by degrees of freedom; SPEC §13.4 uses 2.776 for 5 seeds (4 degrees of freedom)
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228}
KV = re.compile(r"(\w+)=(\S*)")
MEM_UNIT_MB = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}

Cluster = namedtuple("Cluster", "names cores mem_mb")
Job = namedtuple("Job", "job_id trace_id user account submit start end node cores mem_mb runtime state")

# Output columns, in order. The run/diagnostic columns come first, then the metrics by group.
INFO_KEYS = ["run", "window_s", "cluster_nodes", "cluster_cores", "cluster_mem_mb", "jobs_total",
             "jobs_measured", "jobs_missing", "trace_mismatches", "runtime_source", "users_measured"]
JOB_KEYS = ["wait_mean_s", "wait_median_s", "wait_p95_s", "wait_max_s", "bsld_mean",
            "fair_jain_bsld", "fair_wait_spread", "fair_wait_worst_user_s", "fair_wait_best_user_s",
            "makespan_s", "throughput_per_day"]
NODE_KEYS = ["bal_load_sd", "bal_load_max", "util_cores", "util_mem", "stranded_core_h"]
DECISION_KEYS = ["switches", "swap_us_mean", "swap_us_p95", "swap_us_max"]
PLACEMENT_KEYS = ["placements_n", "score_n", "score_us_mean", "score_us_p95", "score_us_max"]
CHURN_KEYS = ["churn_n", "churn_mean", "churn_max"]
COST_KINDS = ("sched", "bf")
COST_KEYS = [f"{k}_{s}" for k in COST_KINDS
             for s in ("n", "us_mean", "us_p95", "us_max", "us_before_switch", "us_after_switch")]
KEYS = INFO_KEYS + JOB_KEYS + NODE_KEYS + DECISION_KEYS + PLACEMENT_KEYS + CHURN_KEYS + COST_KEYS


# ---------------------------------------------------------------- small helpers
def quantile(xs, q):
    """Linear interpolation between the two nearest ranks (numpy's default); None for no data."""
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summary(xs, prefix):
    """{prefix_mean, prefix_p95, prefix_max}, all None when xs is empty."""
    return {f"{prefix}_mean": statistics.fmean(xs) if xs else None,
            f"{prefix}_p95": quantile(xs, 0.95),
            f"{prefix}_max": max(xs) if xs else None}


def bounded_slowdown(wait, runtime, tau=TAU):
    return max(1.0, (wait + runtime) / max(runtime, tau))


def jain(xs):
    """(Σx)² ÷ (n·Σx²); 1 means perfectly even."""
    sq = sum(x * x for x in xs)
    return sum(xs) ** 2 / (len(xs) * sq) if xs and sq else None


def wait_spread(worst, best, tau=TAU):
    """max(1, worst ÷ max(best, τ)): the worst user's mean wait over the best's, floored at τ (SPEC §13.4)."""
    return max(1.0, worst / max(best, tau))


def utc(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def in_window(t, w0, w1):
    return (w0 is None or t >= w0) and (w1 is None or t < w1)


def read_csv(path):
    """Rows as dicts, or None when the file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return None
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- inputs
def expand_hostlist(s):
    """'n[01-03],x5' -> ['n01', 'n02', 'n03', 'x5']."""
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        depth += (ch == "[") - (ch == "]")
        cur += ch
    parts.append(cur)
    names = []
    for part in filter(None, parts):
        m = re.fullmatch(r"([^\[]*)\[([^\]]+)\](.*)", part)
        if not m:
            names.append(part)
            continue
        pre, body, post = m.groups()
        for r in body.split(","):
            a, _, b = r.partition("-")
            names += [f"{pre}{i:0{len(a)}d}{post}" for i in range(int(a), int(b or a) + 1)]
    return names


def read_cluster(slurm_conf):
    """Node names, cores and memory from slurm.conf's NodeName lines; the project cluster if absent."""
    path = Path(slurm_conf)
    if not path.exists():
        n = len(PROJECT_NODES)
        return Cluster(list(PROJECT_NODES), [PROJECT_CORES] * n, [PROJECT_MEM_MB] * n)
    default, names, cores, mem = {}, [], [], []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line.startswith("NodeName="):
            continue
        d = dict(KV.findall(line))
        if d["NodeName"] == "DEFAULT":
            default.update(d)
            continue
        e = {**default, **d}
        if "CPUs" in e or "Procs" in e:
            c = int(e.get("CPUs", e.get("Procs")))
        else:
            c = int(e.get("Sockets", 1)) * int(e.get("CoresPerSocket", 1)) * int(e.get("ThreadsPerCore", 1))
        for name in expand_hostlist(d["NodeName"]):
            names.append(name)
            cores.append(c)
            mem.append(int(e.get("RealMemory", 1)))
    if not names:
        raise ValueError(f"{path}: no NodeName lines")
    return Cluster(names, cores, mem)


def parse_mem_mb(v):
    m = re.fullmatch(r"([\d.]+)([KMGT]?)", v)
    if not m:
        raise ValueError(f"can't read memory '{v}'")
    return float(m[1]) * MEM_UNIT_MB[m[2] or "M"]


def read_jobcomp(path):
    """Completion records as Jobs; runtime is end - start until a trace supplies the real one."""
    jobs = []
    for line in Path(path).read_text().splitlines():
        if not line.startswith("JobId="):
            continue
        d = dict(KV.findall(line))
        if d.get("NodeCnt") != "1":
            raise ValueError(f"job {d['JobId']} ran on {d.get('NodeCnt')} nodes; only single-node jobs are supported (SPEC §3)")
        tres = dict(kv.split("=", 1) for kv in d["Tres"].split(",") if "=" in kv)
        name = d.get("Name", "")
        trace_id = int(name[6:]) if re.fullmatch(r"jobid_\d+", name) else None
        start, end = utc(d["StartTime"]), utc(d["EndTime"])
        jobs.append(Job(int(d["JobId"]), trace_id, d["UserId"].split("(", 1)[0], d.get("Account", ""),
                        utc(d["SubmitTime"]), start, end, d["NodeList"], int(tres.get("cpu", d.get("ProcCnt"))),
                        parse_mem_mb(tres["mem"]), end - start, d.get("JobState", "")))
    return jobs


def load_meta(path):
    """The measured window from meta.json: SPEC §13.2's format, or the P1 speed probe's
    ({time_start, days}, no warm-up)."""
    m = json.loads(Path(path).read_text())
    if "warmup_end" in m:
        return {"window_start": m["window_start"], "warmup_end": m["warmup_end"], "window_end": m["window_end"]}
    if "time_start" in m and "days" in m:
        t = int(m["time_start"])
        return {"window_start": t, "warmup_end": t, "window_end": t + round(m["days"] * 86400)}
    raise ValueError(f"{path}: no warmup_end/window_end (SPEC §13.2) or time_start/days")


def apply_trace(jobs, trace_rows):
    """Take runtimes from jobs.csv; count jobs whose submit, cores or memory disagree with it."""
    by_id = {int(r["job_id"]): r for r in trace_rows}
    out, mismatches = [], 0
    for j in jobs:
        r = by_id.get(j.trace_id)
        if r is None:
            mismatches += 1
            out.append(j)
            continue
        if (int(r["submit"]), int(r["cores"]), int(r["mem_mb"])) != (j.submit, j.cores, j.mem_mb):
            mismatches += 1
        out.append(j._replace(runtime=int(r["runtime_s"])))
    return out, mismatches


# ---------------------------------------------------------------- metrics
def job_metrics(measured):
    """Fairness and general metrics over the measured jobs."""
    out = dict.fromkeys(JOB_KEYS)
    if not measured:
        return out
    waits = [j.start - j.submit for j in measured]
    bsld = [bounded_slowdown(j.start - j.submit, j.runtime) for j in measured]
    by_user = defaultdict(list)
    for j, w, b in zip(measured, waits, bsld):
        by_user[j.user].append((w, b))
    user_wait = [statistics.fmean(w for w, _ in v) for v in by_user.values()]
    user_bsld = [statistics.fmean(b for _, b in v) for v in by_user.values()]
    makespan = max(j.end for j in measured) - min(j.submit for j in measured)
    out.update({
        "wait_mean_s": statistics.fmean(waits), "wait_median_s": quantile(waits, 0.5),
        "wait_p95_s": quantile(waits, 0.95), "wait_max_s": max(waits), "bsld_mean": statistics.fmean(bsld),
        "fair_jain_bsld": jain(user_bsld), "fair_wait_spread": wait_spread(max(user_wait), min(user_wait)),
        "fair_wait_worst_user_s": max(user_wait), "fair_wait_best_user_s": min(user_wait),
        "makespan_s": makespan,
        "throughput_per_day": len(measured) / (makespan / 86400) if makespan > 0 else None,
    })
    return out


def node_metrics(jobs, cluster, w0, w1):
    """Balance, utilization and stranded core-hours, integrated over [w0, w1).

    Sweeps submit, start and end events in time order. All events at one time are applied before
    the state is held until the next event time, so the state is right-continuous.
    """
    idx = {n: i for i, n in enumerate(cluster.names)}
    n_nodes = len(cluster.names)
    cap_c, cap_m = cluster.cores, cluster.mem_mb
    alloc_c, alloc_m = [0] * n_nodes, [0.0] * n_nodes
    pending = []                         # sorted (memory per core, job id) of pending jobs
    acc = dict.fromkeys(("sd", "max", "cores", "mem", "stranded"), 0.0)

    events = []
    for j in jobs:
        if j.node not in idx:
            raise ValueError(f"job {j.job_id} ran on {j.node}, which isn't in the cluster's node list")
        key = (Fraction(j.mem_mb).limit_denominator(1 << 20) / j.cores, j.job_id)
        events += [(j.submit, 0, key, j), (j.start, 1, key, j), (j.end, 2, key, j)]
    events.sort(key=lambda e: (e[0], e[1], e[2][1]))

    def hold(a, b):
        a, b = max(a, w0), min(b, w1)
        if b <= a:
            return
        dt = b - a
        loads = [max(alloc_c[i] / cap_c[i], alloc_m[i] / cap_m[i]) for i in range(n_nodes)]
        mean = sum(loads) / n_nodes
        acc["sd"] += math.sqrt(sum((x - mean) ** 2 for x in loads) / n_nodes) * dt
        acc["max"] += max(loads) * dt
        acc["cores"] += sum(alloc_c) * dt
        acc["mem"] += sum(alloc_m) * dt
        if pending:
            k = len(pending)
            med = pending[k // 2][0] if k % 2 else (pending[k // 2 - 1][0] + pending[k // 2][0]) / 2
            stranded = 0
            for i in range(n_nodes):
                free_c, free_m = cap_c[i] - alloc_c[i], cap_m[i] - alloc_m[i]
                if free_c > 0 and free_m < med * free_c:
                    stranded += free_c
            acc["stranded"] += stranded * dt

    t_prev, i = None, 0
    while i < len(events):
        t = events[i][0]
        if t_prev is not None:
            hold(t_prev, t)
        while i < len(events) and events[i][0] == t:
            _, kind, key, j = events[i]
            n = idx[j.node]
            if kind == 0:
                bisect.insort(pending, key)
            elif kind == 1:
                del pending[bisect.bisect_left(pending, key)]
                alloc_c[n] += j.cores
                alloc_m[n] += j.mem_mb
            else:
                alloc_c[n] -= j.cores
                alloc_m[n] -= j.mem_mb
            i += 1
        t_prev = t
    if t_prev is not None:
        hold(t_prev, w1)

    length = w1 - w0
    return {"bal_load_sd": acc["sd"] / length, "bal_load_max": acc["max"] / length,
            "util_cores": acc["cores"] / (sum(cap_c) * length), "util_mem": acc["mem"] / (sum(cap_m) * length),
            "stranded_core_h": acc["stranded"] / 3600}


def decision_metrics(rows, w0=None, w1=None):
    """Switch count and swap_us statistics; also returns the switch times, for the cost metrics."""
    if rows is None:
        return dict.fromkeys(DECISION_KEYS), None
    rows = [r for r in rows if in_window(int(r["sim_time"]), w0, w1)]
    switched = [r for r in rows if r["switched"] == "1"]
    swap = [float(r["swap_us"]) for r in switched if r["swap_us"] != ""]
    return {"switches": len(switched), **summary(swap, "swap_us")}, [int(r["sim_time"]) for r in switched]


def placement_metrics(rows, w0=None, w1=None):
    if rows is None:
        return dict.fromkeys(PLACEMENT_KEYS)
    rows = [r for r in rows if in_window(int(r["sim_time"]), w0, w1)]
    scored = [float(r["score_us"]) for r in rows if r["score"] != ""]
    return {"placements_n": len(rows), "score_n": len(scored), **summary(scored, "score_us")}


def inversions(seq):
    """Number of pairs i < j with seq[i] > seq[j] (merge sort)."""
    if len(seq) < 2:
        return 0, list(seq)
    mid = len(seq) // 2
    a_inv, a = inversions(seq[:mid])
    b_inv, b = inversions(seq[mid:])
    merged, inv, i, j = [], a_inv + b_inv, 0, 0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            merged.append(a[i])
            i += 1
        else:
            merged.append(b[j])
            inv += len(a) - i
            j += 1
    return inv, merged + a[i:] + b[j:]


def kendall_distance(before, after):
    """Share of pairs of jobs (in both orders) whose relative order flipped; None for < 2 jobs."""
    common = set(before) & set(after)
    pos = {x: i for i, x in enumerate(x for x in before if x in common)}
    seq = [pos[x] for x in after if x in common]
    n = len(seq)
    return inversions(seq)[0] / (n * (n - 1) // 2) if n >= 2 else None


def churn_metrics(rows, w0=None, w1=None):
    if rows is None:
        return dict.fromkeys(CHURN_KEYS)
    dumps = defaultdict(list)
    for r in rows:
        t = int(r["sim_time"])
        if in_window(t, w0, w1):
            dumps[(t, r["phase"])].append((int(r["rank"]), r["job_id"]))
    churn = []
    for t in sorted({t for t, _ in dumps}):
        before, after = ([job for _, job in sorted(dumps.get((t, p), []))] for p in ("before", "after"))
        d = kendall_distance(before, after)
        if d is not None:
            churn.append(d)
    return {"churn_n": len(churn), "churn_mean": statistics.fmean(churn) if churn else None,
            "churn_max": max(churn) if churn else None}


def read_cost(path):
    """{kind: sorted [(sim_utime, real_usec)]} for the sched and bf rows; None if the file is absent.
    A line cut off at the end of a run is skipped."""
    path = Path(path)
    if not path.exists():
        return None
    out = {k: [] for k in COST_KINDS}
    prefixes = tuple(f"{k}," for k in COST_KINDS)
    with open(path) as f:
        for line in f:
            if not line.startswith(prefixes):
                continue
            p = line.rstrip("\n").split(",")
            if len(p) != 6:
                continue
            try:
                out[p[0]].append((int(p[1]), int(p[3])))
            except ValueError:
                continue
    for v in out.values():
        v.sort()
    return out


def cost_metrics(cost, w0, w1, switch_times):
    """Real cycle times (SPEC §8 level 3): over the window, and the cycles adjacent to each switch."""
    if cost is None:
        return dict.fromkeys(COST_KEYS)
    out = {}
    for kind in COST_KINDS:
        rows = cost[kind]
        vals = [real for t, real in rows if in_window(t, w0 * 1_000_000, w1 * 1_000_000)]
        out[f"{kind}_n"] = len(vals)
        out.update(summary(vals, f"{kind}_us"))
        before, after = [], []
        times = [t for t, _ in rows]
        for ts in switch_times or ():
            k = bisect.bisect_left(times, ts * 1_000_000)
            if k > 0:
                before.append(rows[k - 1][1])
            if k < len(rows):
                after.append(rows[k][1])
        out[f"{kind}_us_before_switch"] = statistics.fmean(before) if before else None
        out[f"{kind}_us_after_switch"] = statistics.fmean(after) if after else None
    return out


def run_metrics(run_dir, meta_path, jobs_path=None, label=None):
    """One metrics row (a dict with the KEYS columns) for the run in run_dir."""
    run_dir = Path(run_dir)
    meta = load_meta(meta_path)
    w0, w1 = meta["warmup_end"], meta["window_end"]
    cluster = read_cluster(run_dir / "etc" / "slurm.conf")
    jobs = read_jobcomp(run_dir / "log" / "jobcomp.log")
    jobs_path = Path(jobs_path) if jobs_path else Path(meta_path).with_name("jobs.csv")
    trace = read_csv(jobs_path)
    missing = mismatches = None
    if trace is not None:
        jobs, mismatches = apply_trace(jobs, trace)
        done = {j.trace_id for j in jobs}
        missing = sum(1 for r in trace if w0 <= int(r["submit"]) < w1 and int(r["job_id"]) not in done)
    measured = [j for j in jobs if w0 <= j.submit < w1]

    row = {"run": label or run_dir.name, "window_s": w1 - w0, "cluster_nodes": len(cluster.names),
           "cluster_cores": sum(cluster.cores), "cluster_mem_mb": sum(cluster.mem_mb),
           "jobs_total": len(jobs), "jobs_measured": len(measured), "jobs_missing": missing,
           "trace_mismatches": mismatches, "runtime_source": "jobs.csv" if trace is not None else "jobcomp",
           "users_measured": len({j.user for j in measured})}
    row.update(job_metrics(measured))
    row.update(node_metrics(jobs, cluster, w0, w1))
    dec, switch_times = decision_metrics(read_csv(run_dir / "log" / "decisions.csv"), w0, w1)
    row.update(dec)
    row.update(placement_metrics(read_csv(run_dir / "log" / "placements.csv"), w0, w1))
    row.update(churn_metrics(read_csv(run_dir / "log" / "queue_dumps.csv"), w0, w1))
    row.update(cost_metrics(read_cost(run_dir / "log" / "cost.csv"), w0, w1, switch_times))
    return {k: row[k] for k in KEYS}


# ---------------------------------------------------------------- seeds
def ci95(values):
    """(mean, half-width) with the t quantile for len(values) - 1 degrees of freedom.
    The half-width is None for fewer than 2 values or when a value isn't finite."""
    n = len(values)
    if n == 0:
        return None, None
    m = statistics.fmean(values)
    if n < 2 or not all(math.isfinite(v) for v in values):
        return m, None
    if n - 1 not in T975:
        raise ValueError(f"no t quantile for {n} values (the table covers 2 to {max(T975) + 1})")
    return m, T975[n - 1] * statistics.stdev(values) / math.sqrt(n)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def aggregate(rows):
    """{metric: {"mean", "ci95", "n"}} over seed rows, skipping empty values and text columns."""
    keys = [k for k in KEYS if any(is_number(r.get(k)) for r in rows)]
    keys += sorted({k for r in rows for k, v in r.items() if is_number(v)} - set(keys))
    out = {}
    for k in keys:
        vals = [float(r[k]) for r in rows if is_number(r.get(k))]
        m, h = ci95(vals)
        out[k] = {"mean": m, "ci95": h, "n": len(vals)}
    return out


def paired(rows_a, rows_b):
    """{metric: {"mean", "ci95", "n"}} of the per-seed differences A − B (SPEC §13.4). rows_a[i] and
    rows_b[i] are the same seed; a metric uses the seeds where both rows have a number for it."""
    if len(rows_a) != len(rows_b):
        raise ValueError(f"paired needs one B row per A row ({len(rows_a)} vs {len(rows_b)})")
    pairs = list(zip(rows_a, rows_b))
    both = lambda a, b, k: is_number(a.get(k)) and is_number(b.get(k))
    keys = [k for k in KEYS if any(both(a, b, k) for a, b in pairs)]
    keys += sorted({k for a, b in pairs for k in a if both(a, b, k)} - set(keys))
    out = {}
    for k in keys:
        d = [float(a[k]) - float(b[k]) for a, b in pairs if both(a, b, k)]
        m, h = ci95(d)
        out[k] = {"mean": m, "ci95": h, "n": len(d)}
    return out


def print_ci(agg):
    for k, v in agg.items():
        half = "" if v["ci95"] is None else f" ± {v['ci95']:.6g}"
        print(f"{k:<26} {v['mean']:.6g}{half}  (n={v['n']})")


# ---------------------------------------------------------------- command line
def csv_value(v):
    return "" if v is None else v


def append_csv(path, row):
    path = Path(path)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with open(path, newline="") as f:
            header = next(csv.reader(f), [])
        if header != list(row):
            raise SystemExit(f"{path}: its header doesn't match this row's columns")
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(row)
        w.writerow(csv_value(v) for v in row.values())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "aggregate":
        p = argparse.ArgumentParser(prog="metrics.py aggregate", description="Mean ± 95% CI over seed rows.")
        p.add_argument("rows", nargs="+", type=Path, help="metric rows written by metrics.py --json")
        p.add_argument("--json", type=Path, help="also write the result here")
        a = p.parse_args(argv[1:])
        agg = aggregate([json.loads(f.read_text()) for f in a.rows])
        print_ci(agg)
        if a.json:
            a.json.write_text(json.dumps(agg, indent=1) + "\n")
        return 0
    if argv and argv[0] == "paired":
        p = argparse.ArgumentParser(prog="metrics.py paired",
                                    description="Mean ± 95% CI of the per-seed differences A − B.")
        p.add_argument("--a", nargs="+", type=Path, required=True, help="config A's rows, one per seed")
        p.add_argument("--b", nargs="+", type=Path, required=True, help="config B's rows, same seeds, same order")
        p.add_argument("--json", type=Path, help="also write the result here")
        a = p.parse_args(argv[1:])
        load = lambda files: [json.loads(f.read_text()) for f in files]
        agg = paired(load(a.a), load(a.b))
        print_ci(agg)
        if a.json:
            a.json.write_text(json.dumps(agg, indent=1) + "\n")
        return 0
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", type=Path)
    p.add_argument("--meta", type=Path, required=True, help="the workload's meta.json")
    p.add_argument("--jobs", type=Path, help="the workload's jobs.csv (default: next to --meta)")
    p.add_argument("--label", help="value of the run column (default: RUN_DIR's name)")
    p.add_argument("--json", type=Path, help="also write the row to this file")
    p.add_argument("--csv", type=Path, help="append the row to this CSV file")
    a = p.parse_args(argv)
    row = run_metrics(a.run_dir, a.meta, a.jobs, a.label)
    text = json.dumps(row, indent=1)
    print(text)
    if a.json:
        a.json.write_text(text + "\n")
    if a.csv:
        append_csv(a.csv, row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
