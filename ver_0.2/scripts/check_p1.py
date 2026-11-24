#!/usr/bin/env python3
"""Check the P1 smoke runs against their predictions.

Reads results/p1/<run>/log/jobcomp.log for each run and checks:
  1. weights=age:  jobs 1002-1007 start in submission order
  2. weights=size: jobs start largest first (12, 11, 10, 9, 8, 7 cores)
  3. determinism:  the three age runs give identical start times, end times and nodes
  4. memory:       with 12 GB jobs on 16 GB nodes, only three run at once (one per node)
  5. forks:        the no-op plugin forks give results identical to stock, for both weight sets
  6. clocks:       the fork's probe shows time() is simulated while CLOCK_MONOTONIC stays real
Exit code 0 only if every check passes.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "results" / "p1"
KV = re.compile(r"(\w+)=(\S*)")


def jobs(run):
    path = ROOT / run / "log" / "jobcomp.log"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    out = {}
    for line in path.read_text().splitlines():
        if not line.startswith("JobId="):
            continue
        d = dict(KV.findall(line))
        d["start"] = datetime.fromisoformat(d["StartTime"])
        d["end"] = datetime.fromisoformat(d["EndTime"])
        out[d["Name"]] = d
    return out


def start_order(js, names):
    return [n for n in sorted(names, key=lambda n: (js[n]["start"], n))]


results = []


def check(label, ok, detail):
    results.append((label, ok, detail))


pending = [f"jobid_{i}" for i in range(1002, 1008)]

# 1. age weights -> submission order
age = jobs("priority_age_1")
got = start_order(age, pending)
check("age weights: submission order", got == pending, " ".join(n[-4:] for n in got))

# 2. size weights -> largest first
size = jobs("priority_size")
want = ["jobid_1003", "jobid_1005", "jobid_1007", "jobid_1004", "jobid_1006", "jobid_1002"]
got = start_order(size, pending)
check("size weights: largest first", got == want, " ".join(n[-4:] for n in got))

# 3. determinism across the three age runs
reps = [jobs(f"priority_age_{r}") for r in (1, 2, 3)]
same = all(
    {n: (j["start"], j["end"], j["NodeList"]) for n, j in rep.items()}
    == {n: (j["start"], j["end"], j["NodeList"]) for n, j in reps[0].items()}
    for rep in reps[1:]
)
spread = max(
    (max(r[n]["start"] for r in reps) - min(r[n]["start"] for r in reps)).total_seconds()
    for n in reps[0]
)
check("determinism: 3 age runs identical", same, f"max start-time spread {spread:.0f} s")

# 4. memory is enforced
mem = jobs("memory")
first = [mem[f"jobid_{i}"] for i in (2001, 2002, 2003)]
last = mem["jobid_2004"]
distinct_nodes = len({j["NodeList"] for j in first}) == 3
waited = last["start"] >= min(j["end"] for j in first)
check("memory: 4th 12 GB job waits for a free node", distinct_nodes and waited,
      f"first three on {sorted(j['NodeList'] for j in first)}, 4th started "
      f"{(last['start'] - min(j['start'] for j in first)).total_seconds():.0f} s after the first")

# 5. no-op forks behave exactly like stock
def placement(js):
    return {n: (j["start"], j["end"], j["NodeList"]) for n, j in js.items()}

for w, stock in (("age", age), ("size", size)):
    fork = jobs(f"forks_{w}")
    check(f"forks: identical to stock ({w} weights)", placement(fork) == placement(stock),
          f"{len(fork)} jobs compared")

# 6. clocks: sim time advances much faster than CLOCK_MONOTONIC (which stays real time)
probe = re.compile(r"adaptive_probe: sim_time=(\d+) real_monotonic_us=(\d+)")
ticks = [tuple(map(int, m.groups()))
         for m in probe.finditer((ROOT / "forks_age" / "log" / "slurmctld.log").read_text())]
if len(ticks) >= 2:
    sim_span = ticks[-1][0] - ticks[0][0]
    real_span = (ticks[-1][1] - ticks[0][1]) / 1e6
    ratio = sim_span / real_span if real_span > 0 else float("inf")
    check("clocks: time() simulated, CLOCK_MONOTONIC real", ratio > 2,
          f"{len(ticks)} probes: {sim_span} s simulated over {real_span:.1f} s real ({ratio:.0f}x)")
else:
    check("clocks: time() simulated, CLOCK_MONOTONIC real", False, f"only {len(ticks)} probe lines found")

width = max(len(r[0]) for r in results)
for label, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {label:<{width}}  {detail}")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
