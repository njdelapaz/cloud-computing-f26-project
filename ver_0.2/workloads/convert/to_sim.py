#!/usr/bin/env python3
"""Convert a sampled trace (jobs.csv + meta.json) into simulator input (SPEC §9.2). Standard library only.

Writes into the trace folder (or --out):
  jobs.events      one submit_batch_job event per job; -dt is seconds after meta.json's window_start
  users.sim        user -> uid mapping the simulator needs
  sacctmgr.script  cluster, accounts and users; every account gets the same fair-share
  sim.env          TIME_START for scripts/sim_run.sh. The simulator submits an event at
                   TimeStart + SecondsBeforeFirstJob + dt, and sim_run.sh passes -dtstart 30, so
                   TIME_START = window_start - 30 puts every submit at its real trace time

Each job runs on one node (-N 1, all tasks on that node) with its memory given by --mem in MB, a
time limit in whole minutes (jobs.csv already holds whole minutes) and its recorded runtime as
-sim-walltime. The job name jobid_<job_id> ties simulator output back to jobs.csv.

Usage: python3 -I workloads/convert/to_sim.py TRACE_DIR [--out DIR] [--cluster project]
Supersedes the one-off converter in scripts/p1_speed_probe.py.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ACCOUNT_FAIRSHARE = 100       # same for every account (SPEC §9, §11 #5)
FIRST_UID = 2000
LEAD_S = 30                   # SecondsBeforeFirstJob: scripts/sim_run.sh runs slurmsim with -dtstart 30


def event_line(job, window_start):
    minutes = int(job["time_limit_s"]) // 60
    cores = int(job["cores"])
    return (f"-dt {int(job['submit']) - window_start} -e submit_batch_job | "
            f"-J jobid_{job['job_id']} -sim-walltime {int(job['runtime_s'])} --uid={job['user']} "
            f"-t {minutes} -N 1 -n {cores} --ntasks-per-node={cores} --mem={int(job['mem_mb'])} "
            f"-A {job['account']} -p normal -q normal pseudo.job\n")


def convert(trace_dir, out_dir=None, cluster="project", lead=LEAD_S):
    trace_dir = Path(trace_dir)
    out = Path(out_dir) if out_dir else trace_dir
    meta = json.loads((trace_dir / "meta.json").read_text())
    with open(trace_dir / "jobs.csv", newline="") as f:
        jobs = list(csv.DictReader(f))
    if not jobs:
        raise SystemExit("jobs.csv has no jobs")
    t0 = meta["window_start"]

    out.mkdir(parents=True, exist_ok=True)
    with open(out / "jobs.events", "w") as f:
        for j in jobs:
            if int(j["time_limit_s"]) % 60:
                raise SystemExit(f"job {j['job_id']}: time_limit_s isn't whole minutes")
            f.write(event_line(j, t0))

    per_user = defaultdict(Counter)
    for j in jobs:
        per_user[j["user"]][j["account"]] += 1
    users = sorted(per_user)
    accounts = sorted({j["account"] for j in jobs})

    with open(out / "users.sim", "w") as f:
        f.write("admin:1000:admin:1000\n")
        for k, u in enumerate(users):
            f.write(f"{u}:{FIRST_UID + k}:users:100\n")

    with open(out / "sacctmgr.script", "w") as f:
        f.write("modify QOS set normal Priority=0\n")
        f.write(f"add cluster Name={cluster} Fairshare=1 QOS=normal\n")
        for acc in accounts:
            f.write(f"add account name={acc} Fairshare={ACCOUNT_FAIRSHARE}\n")
        f.write(f"add user name=admin DefaultAccount={accounts[0]} AdminLevel=Administrator\n")
        for u in users:
            # default account: the one the user ran most jobs under (ties by name)
            accs = sorted(per_user[u], key=lambda a: (-per_user[u][a], a))
            f.write(f"add user name={u} DefaultAccount={accs[0]}\n")
            for acc in sorted(accs[1:]):
                f.write(f"add user name={u} Account={acc}\n")
        f.write("list associations format=Account,Cluster,User,Fairshare tree withd\n")

    (out / "sim.env").write_text(f"TIME_START={t0 - lead}\n")
    return {"jobs": len(jobs), "users": len(users), "accounts": len(accounts), "time_start": t0 - lead,
            "out": str(out)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace_dir", help="folder holding jobs.csv and meta.json from sample.py")
    ap.add_argument("--out", help="output folder (default: the trace folder)")
    ap.add_argument("--cluster", default="project", help="ClusterName in slurm.conf")
    ap.add_argument("--lead", type=int, default=LEAD_S, help="the run's SecondsBeforeFirstJob (-dtstart)")
    a = ap.parse_args()
    print(json.dumps(convert(a.trace_dir, a.out, a.cluster, a.lead)))


if __name__ == "__main__":
    sys.exit(main())
