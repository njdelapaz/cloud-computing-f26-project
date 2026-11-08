"""The common workload format, shared by the generator, converter and metrics.

A workload is a CSV file with one row per job:

    job_id,user,submit_time,gpus,runtime,time_limit

  job_id       integer, unique within the workload
  user         a user in sim/etc/users.sim
  submit_time  seconds after the first submission (the first job is at 0)
  gpus         GPUs requested, 1-24
  runtime      how long the job actually runs, in seconds; a job whose
               runtime is at least its time_limit ends as TIMEOUT
  time_limit   the time limit the user requests, in seconds; Slurm counts
               limits in whole minutes, so this should be a multiple of 60

Only the Python standard library is used, so the scripts run without a
virtual environment.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

FIELDS = ["job_id", "user", "submit_time", "gpus", "runtime", "time_limit"]

REPO = Path(__file__).resolve().parent.parent
SACCTMGR_SCRIPT = REPO / "sim" / "etc" / "sacctmgr.script"
USERS_SIM = REPO / "sim" / "etc" / "users.sim"

GPUS_PER_NODE = 8
NODES = ["gpu1", "gpu2", "gpu3"]
TOTAL_GPUS = GPUS_PER_NODE * len(NODES)


@dataclass
class Job:
    job_id: int
    user: str
    submit_time: int
    gpus: int
    runtime: int
    time_limit: int

    @property
    def name(self):
        """Job name used in the events file and reported back by sacct."""
        return f"jobid_{self.job_id}"

    @property
    def nodes(self):
        """Fewest nodes that hold this many GPUs."""
        return -(-self.gpus // GPUS_PER_NODE)

    @property
    def times_out(self):
        return self.runtime >= self.time_limit


def read_workload(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            raise ValueError(f"{path}: header must be {','.join(FIELDS)}, got {reader.fieldnames}")
        jobs = [Job(**{k: (row[k] if k == "user" else int(row[k])) for k in FIELDS}) for row in reader]
    validate(jobs, path)
    return jobs


def write_workload(jobs, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(FIELDS)
        for j in jobs:
            w.writerow([j.job_id, j.user, j.submit_time, j.gpus, j.runtime, j.time_limit])


def validate(jobs, where="workload"):
    users = sim_users()
    seen = set()
    for j in jobs:
        problem = None
        if j.job_id in seen:
            problem = "duplicate job_id"
        elif j.user not in users:
            problem = f"unknown user {j.user!r} (not in sim/etc/users.sim)"
        elif not 1 <= j.gpus <= TOTAL_GPUS:
            problem = f"gpus must be 1-{TOTAL_GPUS}"
        elif j.submit_time < 0:
            problem = "negative submit_time"
        elif j.runtime <= 0:
            problem = "runtime must be positive"
        elif j.time_limit < 60:
            problem = "time_limit must be at least 60 s"
        if problem:
            raise ValueError(f"{where}: job {j.job_id}: {problem}")
        seen.add(j.job_id)


def sim_users():
    """User names defined for the simulator (sim/etc/users.sim)."""
    return {line.split(":")[0] for line in USERS_SIM.read_text().split() if line}


def user_accounts():
    """Map each user to their default account, from sim/etc/sacctmgr.script."""
    accounts = {}
    for line in SACCTMGR_SCRIPT.read_text().splitlines():
        m = re.match(r"\s*add user name=(\S+) DefaultAccount=(\S+)", line)
        if m:
            accounts[m.group(1)] = m.group(2)
    return accounts
