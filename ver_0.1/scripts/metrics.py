#!/usr/bin/env python3
"""Compute scheduling metrics for one simulator run.

Usage:
  scripts/metrics.py <results-dir> <workload.csv> [--csv SUMMARY.csv] [--setup NAME]

  results-dir   a run's output, e.g. sim/results/baseline_light/dtstart_59_1
  workload.csv  the workload the run used, in the common format; GPU counts
                come from here because slurm_acct.out does not record them

Writes <results-dir>/metrics.json (every metric plus per-user and per-node
detail) and prints a short report. With --csv, also adds or replaces this
run's row in a summary CSV, one row per run.

Metrics, all over the run's makespan (first submission to last job end):

  Utilization  gpu_util            GPU-seconds used / (24 GPUs x makespan)
               idle_gpu_hours      GPU-hours left idle during the makespan
  Performance  wait_mean/p50/p95   start - submit, in seconds
               turnaround_mean     end - submit, in seconds
               makespan            seconds
               bsld_mean/p95       bounded slowdown,
                                   max(1, (wait + run) / max(run, 10 s))
  Balance      node_gpu_var_mean   time-averaged variance of GPUs in use
                                   across the three nodes (0 = even)
               node_gpu_imbalance_mean  time-averaged (busiest - idlest node)
               wait_std            standard deviation of wait times
  Fairness     jain_bsld           Jain's index over per-user mean bounded
                                   slowdown (1 = every user equally served)
               jain_wait           Jain's index over per-user mean wait

Per-node use is rebuilt by replaying job starts and ends. slurm_acct.out
lists a job's nodes but not its GPUs per node, so a multi-node job whose GPU
count isn't a multiple of 8 is split as evenly as the free GPUs allow;
`approx_split_jobs` counts these.
"""

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

from workload_format import GPUS_PER_NODE, NODES, TOTAL_GPUS, read_workload

BSLD_TAU = 10  # seconds; the usual bounded-slowdown threshold
CPUS_PER_GPU = 8  # DefCpuPerGPU in the policy files


def parse_time(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None  # "Unknown" / "None" for jobs that never started


def expand_nodelist(text):
    """'gpu[1,3]' -> ['gpu1', 'gpu3']; 'gpu[1-3]' -> all three; 'gpu2' -> ['gpu2']."""
    m = re.fullmatch(r"([a-z]+)\[([\d,\-]+)\]", text)
    if not m:
        return [text] if text and text != "None assigned" else []
    prefix, out = m.group(1), []
    for part in m.group(2).split(","):
        lo, _, hi = part.partition("-")
        out += [f"{prefix}{i}" for i in range(int(lo), int(hi or lo) + 1)]
    return out


def read_acct(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="|"))


def percentile(values, p):
    """Nearest-rank percentile."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, -(-p * len(s) // 100) - 1))
    return s[int(k)]


def jain(values):
    if not values:
        return None
    sq = sum(v * v for v in values)
    return 1.0 if sq == 0 else sum(values) ** 2 / (len(values) * sq)


def split_gpus(gpus, nodes, free):
    """GPUs per node for a job on `nodes`, and whether the split is a guess."""
    n = len(nodes)
    if n == 1:
        return {nodes[0]: gpus}, False
    if gpus == GPUS_PER_NODE * n:
        return {node: GPUS_PER_NODE for node in nodes}, False
    alloc = {node: gpus // n + (1 if i < gpus % n else 0) for i, node in enumerate(nodes)}
    # Move any excess off nodes that don't have that many GPUs free.
    excess = 0
    for node in nodes:
        over = alloc[node] - free[node]
        if over > 0:
            alloc[node] -= over
            excess += over
    for node in nodes:
        give = min(excess, free[node] - alloc[node])
        alloc[node] += give
        excess -= give
    return alloc, True


def node_usage(runs, t0, t1):
    """Replay starts/ends; return per-node GPU-seconds and balance over [t0, t1]."""
    events = []  # (time, kind, job); ends (0) before starts (1) at the same time
    for r in runs:
        events.append((r["start"], 1, r))
        events.append((r["end"], 0, r))
    events.sort(key=lambda e: (e[0], e[1]))

    in_use = {n: 0 for n in NODES}
    gpu_s = {n: 0.0 for n in NODES}
    var_area = imb_area = 0.0
    approx = overcommit = 0
    last = t0
    for t, kind, r in events:
        dt = t - last
        if dt > 0:
            vals = list(in_use.values())
            var_area += statistics.pvariance(vals) * dt
            imb_area += (max(vals) - min(vals)) * dt
            for n in NODES:
                gpu_s[n] += in_use[n] * dt
            last = t
        if kind == 1:
            free = {n: GPUS_PER_NODE - in_use[n] for n in NODES}
            alloc, guessed = split_gpus(r["gpus"], r["nodes"], free)
            approx += guessed
            r["alloc"] = alloc
            for n, g in alloc.items():
                in_use[n] += g
                overcommit += in_use[n] > GPUS_PER_NODE
        else:
            for n, g in r["alloc"].items():
                in_use[n] -= g
    span = max(t1 - t0, 1)
    return {
        "node_gpu_hours": {n: round(gpu_s[n] / 3600, 2) for n in NODES},
        "node_gpu_var_mean": var_area / span,
        "node_gpu_imbalance_mean": imb_area / span,
        "approx_split_jobs": approx,
        "overcommit_events": overcommit,
    }


def compute(results_dir, workload_path):
    jobs = {j.name: j for j in read_workload(workload_path)}
    rows = read_acct(Path(results_dir) / "slurm_acct.out")

    runs, states, mismatched = [], {}, []
    seen = set()
    for row in rows:
        name = row["JobName"]
        if name not in jobs:
            continue
        seen.add(name)
        states[row["State"]] = states.get(row["State"], 0) + 1
        job = jobs[name]
        submit, start, end = (parse_time(row[k]) for k in ("Submit", "Start", "End"))
        if start is None or end is None:
            continue
        if int(row["NCPUS"]) != CPUS_PER_GPU * job.gpus:
            mismatched.append(name)
        runs.append({
            "name": name, "user": job.user, "gpus": job.gpus,
            "submit": submit, "start": start, "end": end,
            "wait": start - submit, "run": end - start,
            "nodes": expand_nodelist(row["NodeList"]),
        })
    if not runs:
        raise SystemExit(f"{results_dir}: no finished jobs from {workload_path}")

    for r in runs:
        r["bsld"] = max(1.0, (r["wait"] + r["run"]) / max(r["run"], BSLD_TAU))

    t0 = min(r["submit"] for r in runs)
    t1 = max(r["end"] for r in runs)
    makespan = t1 - t0
    used = sum(r["gpus"] * r["run"] for r in runs)
    waits = [r["wait"] for r in runs]
    bslds = [r["bsld"] for r in runs]

    per_user = {}
    for r in runs:
        u = per_user.setdefault(r["user"], {"jobs": 0, "gpu_hours": 0.0, "wait": [], "bsld": []})
        u["jobs"] += 1
        u["gpu_hours"] += r["gpus"] * r["run"] / 3600
        u["wait"].append(r["wait"])
        u["bsld"].append(r["bsld"])
    users = {
        name: {
            "jobs": u["jobs"],
            "gpu_hours": round(u["gpu_hours"], 2),
            "wait_mean": statistics.fmean(u["wait"]),
            "bsld_mean": statistics.fmean(u["bsld"]),
        }
        for name, u in sorted(per_user.items())
    }

    m = {
        "jobs_in_workload": len(jobs),
        "jobs_finished": len(runs),
        "jobs_missing": len(jobs) - len(seen),
        "states": dict(sorted(states.items())),
        "gpu_count_mismatches": len(mismatched),
        "makespan": makespan,
        "gpu_util": used / (TOTAL_GPUS * makespan),
        "idle_gpu_hours": (TOTAL_GPUS * makespan - used) / 3600,
        "wait_mean": statistics.fmean(waits),
        "wait_p50": percentile(waits, 50),
        "wait_p95": percentile(waits, 95),
        "wait_max": max(waits),
        "wait_std": statistics.pstdev(waits),
        "turnaround_mean": statistics.fmean(r["end"] - r["submit"] for r in runs),
        "bsld_mean": statistics.fmean(bslds),
        "bsld_p95": percentile(bslds, 95),
        "jain_bsld": jain([u["bsld_mean"] for u in users.values()]),
        "jain_wait": jain([u["wait_mean"] for u in users.values()]),
    }
    m.update(node_usage(runs, t0, t1))
    m["users"] = users
    return m


SUMMARY_FIELDS = [
    "run", "setup", "workload", "jobs_finished", "makespan", "gpu_util", "idle_gpu_hours",
    "wait_mean", "wait_p50", "wait_p95", "wait_std", "turnaround_mean", "bsld_mean", "bsld_p95",
    "node_gpu_var_mean", "node_gpu_imbalance_mean", "jain_bsld", "jain_wait",
]


def upsert_summary(path, row):
    path = Path(path)
    rows = []
    if path.exists():
        with open(path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["run"] != row["run"]]
    rows.append(row)
    rows.sort(key=lambda r: r["run"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def fmt(v):
    return f"{v:.4f}" if isinstance(v, float) else v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_dir")
    ap.add_argument("workload")
    ap.add_argument("--csv", help="summary CSV to add this run's row to")
    ap.add_argument("--setup", default="", help="setup label for the summary row, e.g. baseline")
    args = ap.parse_args()

    m = compute(args.results_dir, args.workload)
    out = Path(args.results_dir) / "metrics.json"
    out.write_text(json.dumps(m, indent=2) + "\n")

    problems = []
    if m["jobs_missing"] or m["jobs_finished"] != m["jobs_in_workload"]:
        problems.append(f"{m['jobs_in_workload'] - m['jobs_finished']} of "
                        f"{m['jobs_in_workload']} jobs did not finish")
    if m["gpu_count_mismatches"]:
        problems.append(f"{m['gpu_count_mismatches']} jobs got a CPU count that doesn't match their GPUs")
    if m["overcommit_events"]:
        problems.append(f"per-node replay overcommitted a node {m['overcommit_events']} times")

    print(f"{args.results_dir}: {m['jobs_finished']} jobs, makespan {m['makespan'] / 3600:.2f} h")
    print(f"  utilization  {m['gpu_util']:.3f}   idle GPU-h {m['idle_gpu_hours']:.1f}")
    print(f"  wait (s)     mean {m['wait_mean']:.0f}  p95 {m['wait_p95']:.0f}  "
          f"bsld mean {m['bsld_mean']:.2f}  p95 {m['bsld_p95']:.2f}")
    print(f"  balance      node var {m['node_gpu_var_mean']:.2f}  "
          f"imbalance {m['node_gpu_imbalance_mean']:.2f} GPUs  wait std {m['wait_std']:.0f} s")
    print(f"  fairness     Jain bsld {m['jain_bsld']:.3f}  Jain wait {m['jain_wait']:.3f}")
    for p in problems:
        print(f"  warning: {p}", file=sys.stderr)

    if args.csv:
        rd = Path(args.results_dir).resolve()
        row = {k: fmt(m[k]) for k in SUMMARY_FIELDS if k in m}
        row.update(run=f"{rd.parent.name}/{rd.name}", setup=args.setup,
                   workload=Path(args.workload).stem)
        upsert_summary(args.csv, row)


if __name__ == "__main__":
    main()
