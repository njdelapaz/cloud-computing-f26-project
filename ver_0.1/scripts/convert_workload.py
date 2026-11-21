#!/usr/bin/env python3
"""Convert a workload between the common CSV format and the simulator's events format.

Usage:
  scripts/convert_workload.py <workload.csv> [-o out.events] [--switch SECONDS:POLICY ...]
  scripts/convert_workload.py --from-events <in.events> [-o out.csv]

The default direction writes an events file for scripts/run_sim.sh. Each job
becomes one submit_batch_job line, following the request form Phase 1 found
to work (see sim/README.md):

  -N <fewest nodes> --gpus=<G>   the simulator rejects --gpus-per-task, and
                                 without -N a small job can split across nodes
  -sim-walltime <runtime>        -1 when the job runs into its time limit
                                 (runtime >= time_limit); 0 would end it at once
  -t <time_limit>                rounded up to whole minutes, as Slurm counts them
  -A <account>                   the user's default account in sacctmgr.script

--switch adds a switch_policy event at the given simulated second, so a
scheduled policy switch can be layered on any workload without editing it.

--from-events reads job lines back into the common format (switch events and
comments are skipped). It exists so hand-written events files, such as the
Phase 1 tests, can be checked against the converter.
"""

import argparse
import re
import sys
from pathlib import Path

from workload_format import Job, read_workload, user_accounts, validate, write_workload

POLICIES = Path(__file__).resolve().parent.parent / "sim" / "etc" / "policies"


def format_limit(seconds):
    """Slurm time limit string, rounded up to whole minutes."""
    minutes = -(-seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    hms = f"{hours:02d}:{minutes:02d}:00"
    return f"{days}-{hms}" if days else hms


def parse_limit(text):
    """Seconds in a Slurm time limit string ([D-]HH:MM:SS)."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    h, m, s = (int(x) for x in text.split(":"))
    return ((days * 24 + h) * 60 + m) * 60 + s


def job_line(job, account):
    walltime = -1 if job.times_out else job.runtime
    return (
        f"-dt {job.submit_time} -e submit_batch_job | -J {job.name} -sim-walltime {walltime}"
        f" --uid={job.user} -t {format_limit(job.time_limit)} -N {job.nodes} --gpus={job.gpus}"
        f" -A {account} -p gpu -q normal pseudo.job"
    )


def to_events(jobs, switches):
    accounts = user_accounts()
    events = []  # (time, order, line); switches sort before jobs at the same time
    for t, policy in switches:
        events.append((t, 0, f"-dt {t} -e switch_policy | {policy}"))
    for j in jobs:
        if j.user not in accounts:
            raise ValueError(f"job {j.job_id}: user {j.user!r} has no account in sacctmgr.script")
        if j.time_limit % 60:
            print(f"warning: job {j.job_id}: time_limit {j.time_limit}s rounded up to "
                  f"{format_limit(j.time_limit)}", file=sys.stderr)
        events.append((j.submit_time, 1, job_line(j, accounts[j.user])))
    events.sort(key=lambda e: (e[0], e[1]))
    return [line for _, _, line in events]


JOB_RE = re.compile(
    r"^-dt\s+(?P<dt>\d+)\s+-e\s+submit_batch_job\s*\|\s*(?P<args>.*)$"
)


def from_events(path):
    jobs = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = JOB_RE.match(line)
        if not m:
            continue  # switch_policy and other events
        args = m.group("args")

        def opt(pattern):
            found = re.search(pattern, args)
            if not found:
                raise ValueError(f"{path}:{n}: missing {pattern!r}")
            return found.group(1)

        limit = parse_limit(opt(r"-t\s+(\S+)"))
        walltime = int(opt(r"-sim-walltime\s+(-?\d+)"))
        jobs.append(Job(
            job_id=int(opt(r"-J\s+jobid_(\d+)")),
            user=opt(r"--uid=(\S+)"),
            submit_time=int(m.group("dt")),
            gpus=int(opt(r"--gpus=(\d+)")),
            runtime=limit if walltime == -1 else walltime,
            time_limit=limit,
        ))
    validate(jobs, path)
    return jobs


def parse_switch(text):
    t, _, policy = text.partition(":")
    if not t.isdigit() or not policy:
        raise argparse.ArgumentTypeError(f"expected SECONDS:POLICY, got {text!r}")
    if not (POLICIES / f"{policy}.conf").is_file():
        raise argparse.ArgumentTypeError(f"no policy file sim/etc/policies/{policy}.conf")
    return int(t), policy


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="workload CSV (or events file with --from-events)")
    ap.add_argument("-o", "--output", help="output file (default: input with .events / .csv)")
    ap.add_argument("--switch", action="append", type=parse_switch, default=[],
                    metavar="SECONDS:POLICY", help="add a switch_policy event (repeatable)")
    ap.add_argument("--from-events", action="store_true", help="convert an events file back to CSV")
    args = ap.parse_args()

    src = Path(args.input)
    if args.from_events:
        out = Path(args.output) if args.output else src.with_suffix(".csv")
        jobs = from_events(src)
        write_workload(jobs, out)
    else:
        out = Path(args.output) if args.output else src.with_suffix(".events")
        jobs = read_workload(src)
        out.write_text("\n".join(to_events(jobs, args.switch)) + "\n")
    print(f"{len(jobs)} jobs -> {out}")


if __name__ == "__main__":
    main()
