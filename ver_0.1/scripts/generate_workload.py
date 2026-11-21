#!/usr/bin/env python3
"""Generate a synthetic workload from a seeded JSON spec.

Usage:
  scripts/generate_workload.py <spec.json> [<spec.json> ...] [-o OUTDIR]
  scripts/generate_workload.py --all

Writes <name>.csv (common format, see workload_format.py) and <name>.events
(simulator input) to OUTDIR, default sim/workloads/. --all regenerates every
spec in sim/workloads/specs/. The same spec always gives the same files.

Spec format:

  {
    "name": "heavy_small",
    "seed": 2,
    "phases": [                      # run back to back; at least one
      {
        "duration": 43200,           # seconds of submissions in this phase
        "load": 1.1,                 # offered load: GPU-seconds submitted per
                                     # GPU-second of cluster capacity
        "bursts": {                  # optional: arrival rate x multiplier for
          "every": 7200,             #   `length` seconds out of every `every`
          "length": 900,             #   seconds; the average rate still
          "multiplier": 4            #   matches `load`
        },
        "gpus": {"1": 0.4, "2": 0.3, "4": 0.3},     # GPU-count weights
        "runtime": {"median": 1800, "sigma": 1.0,   # lognormal, in seconds,
                    "min": 60, "max": 21600},       #   clamped to [min, max]
        "time_limit_factor": [1.2, 3.0],  # limit = runtime x uniform(lo, hi),
                                          #   rounded up to whole minutes
        "timeout_fraction": 0.05,    # share of jobs that run into their limit
        "users": {"user1": 1, "user2": 1}           # share of jobs per user
      }
    ]
  }

Arrivals are a Poisson process. Its rate comes from `load`: rate =
load x 24 GPUs / E[gpus x runtime], with E[runtime] estimated from 50,000
samples of a separately seeded generator, so it is also reproducible.
"""

import argparse
import json
import math
import random
import sys
from pathlib import Path

from convert_workload import to_events
from workload_format import TOTAL_GPUS, Job, validate, write_workload

REPO = Path(__file__).resolve().parent.parent
SPECS = REPO / "sim" / "workloads" / "specs"
FIRST_JOB_ID = 1001


def draw_runtime(rng, r):
    mu = math.log(r["median"])
    return int(round(min(max(rng.lognormvariate(mu, r["sigma"]), r["min"]), r["max"])))


def weighted(rng, weights):
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def mean_runtime(seed, index, r, samples=50_000):
    rng = random.Random(f"{seed}-phase{index}-runtime-estimate")
    return sum(draw_runtime(rng, r) for _ in range(samples)) / samples


def phase_jobs(rng, seed, index, phase, t0, next_id):
    gpus_w = {int(k): v for k, v in phase["gpus"].items()}
    total_w = sum(gpus_w.values())
    mean_gpus = sum(g * w for g, w in gpus_w.items()) / total_w
    rate = phase["load"] * TOTAL_GPUS / (mean_gpus * mean_runtime(seed, index, phase["runtime"]))

    bursts = phase.get("bursts")
    if bursts:
        mult = bursts["multiplier"]
        base = rate / (1 + (mult - 1) * bursts["length"] / bursts["every"])
        peak = base * mult
        in_burst = lambda t: (t % bursts["every"]) < bursts["length"]
    else:
        base = peak = rate
        in_burst = lambda t: False

    lo, hi = phase.get("time_limit_factor", [1.5, 1.5])
    timeout_fraction = phase.get("timeout_fraction", 0.0)
    jobs = []
    t = 0.0
    while True:
        # Thinning: candidates at the peak rate, kept with probability rate(t)/peak.
        t += rng.expovariate(peak)
        if t >= phase["duration"]:
            break
        if rng.random() * peak > (peak if in_burst(t) else base):
            continue
        gpus = weighted(rng, gpus_w)
        runtime = draw_runtime(rng, phase["runtime"])
        factor = rng.uniform(lo, hi)
        if rng.random() < timeout_fraction:
            # The user underestimated: the job hits its limit and is killed.
            limit = max(60, math.ceil(runtime * rng.uniform(0.5, 0.9) / 60) * 60)
            runtime = limit
        else:
            limit = max(60, math.ceil(runtime * factor / 60) * 60)
            if limit <= runtime:
                limit += 60
        jobs.append(Job(next_id + len(jobs), weighted(rng, phase["users"]),
                        int(t0 + t), gpus, runtime, limit))
    return jobs


def generate(spec):
    seed = spec["seed"]
    rng = random.Random(seed)
    jobs, t0 = [], 0
    for i, phase in enumerate(spec["phases"]):
        jobs += phase_jobs(rng, seed, i, phase, t0, FIRST_JOB_ID + len(jobs))
        t0 += phase["duration"]
    if not jobs:
        raise ValueError(f"{spec['name']}: no jobs generated")
    # The common format starts the clock at the first submission.
    first = jobs[0].submit_time
    for j in jobs:
        j.submit_time -= first
    validate(jobs, spec["name"])
    return jobs, t0


def summary(name, jobs, duration):
    gpu_s = sum(j.gpus * j.runtime for j in jobs)
    users = {}
    for j in jobs:
        users[j.user] = users.get(j.user, 0) + 1
    sizes = {"1-8": 0, "9-16": 0, "17-24": 0}
    for j in jobs:
        sizes["1-8" if j.gpus <= 8 else "9-16" if j.gpus <= 16 else "17-24"] += 1
    return (f"{name}: {len(jobs)} jobs over {duration / 3600:.1f} h, offered load "
            f"{gpu_s / (TOTAL_GPUS * duration):.2f}, "
            f"timeouts {sum(j.times_out for j in jobs)}, "
            f"sizes {sizes}, users {dict(sorted(users.items()))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("specs", nargs="*", help="spec JSON files")
    ap.add_argument("--all", action="store_true", help=f"every spec in {SPECS.relative_to(REPO)}/")
    ap.add_argument("-o", "--outdir", default=str(REPO / "sim" / "workloads"))
    args = ap.parse_args()
    paths = sorted(SPECS.glob("*.json")) if args.all else [Path(p) for p in args.specs]
    if not paths:
        ap.error("give spec files or --all")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        spec = json.loads(Path(path).read_text())
        jobs, duration = generate(spec)
        write_workload(jobs, outdir / f"{spec['name']}.csv")
        (outdir / f"{spec['name']}.events").write_text("\n".join(to_events(jobs, [])) + "\n")
        print(summary(spec["name"], jobs, duration))


if __name__ == "__main__":
    sys.exit(main())
