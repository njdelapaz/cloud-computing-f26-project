"""Task 1.2 mid-run probe: can the simulator be queried and reconfigured while it runs?

Usage: .venv/bin/python -m sim.probe

Builds a run where every node is busy for hours of simulated time while 60 jobs from three
accounts wait, starts it detached, and keeps trying the Slurm CLI inside the container until
the simulation ends:
  Q1  Do squeue, sprio, sshare and scontrol show node answer mid-run?
  Q2  Does `scontrol reconfigure` (needed to apply new weights) work mid-run?
  Q3  Does the run still complete correctly after those attempts?
  Q4  How can an outside process read simulated time?
It also checks that DebugFlags=Priority writes per-job priority factors to slurmctld.log,
which replaces sprio for verifying weight configurations (task 1.7).

Writes results/probe/probe.json; the raw run stays in results/probe/run1/ (gitignored).
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import time
from pathlib import Path

from sim.check_run import check
from sim.prepare_run import ROOT, prepare_run

RUN = Path("results/probe/run1")
OUT = ROOT / "results" / "probe"
CONF = f"/work/{RUN}/etc/slurm.conf"
CONTROLLER_SRC = ROOT / "third_party" / "slurm_simulator" / "contribs" / "sim" / "slurmctld_controller.c"
TOOLS = {
    "squeue": ["squeue"],
    "sprio": ["sprio", "-l"],
    "sshare": ["sshare", "-l"],
    "scontrol show node": ["scontrol", "show", "node", "n01"],
    "scontrol reconfigure": ["scontrol", "reconfigure"],
}


def make_workload(path_events: Path, path_accounts: Path) -> None:
    rng = random.Random(7)
    users = [("u1", "acct1"), ("u2", "acct1"), ("u3", "acct2"), ("u4", "acct3")]
    jobs = [(0, "u1", "acct1", 32, 120000, 300, 4 * 3600) for _ in range(16)]
    for i in range(60):
        u, a = users[0] if i % 2 == 0 else rng.choice(users[1:])
        cpus = rng.choice([1, 2, 4, 8, 16])
        mem = cpus * rng.choice([2000, 4000, 6000])
        limit = rng.choice([30, 120, 600, 1440, 4320])
        jobs.append((60 + i, u, a, cpus, mem, limit, min(limit * 60, rng.randint(600, 7200))))
    lines = [f"-dt {dt} -e submit_batch_job | -J jobid_{1001 + i} -sim-walltime {r} --uid={u} "
             f"-t {t} -n {c} --ntasks-per-node={c} -N 1 --mem={m} -A {a} -p normal -q normal pseudo.job"
             for i, (dt, u, a, c, m, t, r) in enumerate(jobs)]
    path_events.write_text("\n".join(lines) + "\n")
    path_accounts.write_text("".join(f"{a}\t{u}\n" for u, a in users))


def dexec(name: str, cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-u", "slurm", "-e", f"SLURM_CONF={CONF}", name, *cmd],
                          capture_output=True, text=True, timeout=60)


def running(name: str) -> bool:
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                       capture_output=True, text=True)
    return r.stdout.strip() == "true"


def main() -> None:
    (ROOT / RUN.parent).mkdir(parents=True, exist_ok=True)
    ev_path, ac_path = ROOT / RUN.parent / "_events.txt", ROOT / RUN.parent / "_accounts.txt"
    make_workload(ev_path, ac_path)
    prepare_run(RUN, ev_path, ac_path, ROOT / "config" / "priority" / "B0.conf", 1_716_163_200,
                extra_conf=["DebugFlags=Priority"])
    ev_path.unlink()
    ac_path.unlink()

    t0 = time.time()
    name = subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(RUN), "30", "--detach"],
                          capture_output=True, text=True, check=True).stdout.strip()

    # Q1/Q2: keep trying every tool until the simulation ends.
    attempts = {k: {"tries": 0, "ok": 0, "messages": {}} for k in TOOLS}
    while running(name):
        for label, cmd in TOOLS.items():
            r = dexec(name, cmd)
            a = attempts[label]
            a["tries"] += 1
            if r.returncode == 0 and r.stdout.strip():
                a["ok"] += 1
            else:
                msg = (r.stderr.strip() or "no output").splitlines()[0][:160]
                if "is not running" in msg:      # container ended between checks
                    a["tries"] -= 1
                    continue
                a["messages"][msg] = a["messages"].get(msg, 0) + 1
        time.sleep(1)
    wall = time.time() - t0
    subprocess.run(["docker", "rm", name], capture_output=True)

    # Q3: the run itself.
    log = (ROOT / RUN / "run.log").read_text()
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
    speed = {k: (re.search(rf"{k}: (\S+)", clean) or [None, None])[1]
             for k in ["Simulated time", "Real time", "Acceleration"]}
    problems = check(RUN)

    # Priority logging (sprio replacement).
    ctld = (ROOT / RUN / "out" / "slurmctld.log").read_text()
    prio_lines = [l for l in ctld.splitlines() if "_get_priority_internal: Job " in l]
    factor_lines = [l for l in ctld.splitlines() if "_get_priority_internal: Weighted" in l]

    # Q4: source evidence for how the simulated controller treats the RPC thread.
    src = CONTROLLER_SRC.read_text().splitlines()
    i = next(k for k, l in enumerate(src) if "thread_id_rpc" in l)
    rpc_src = [l.rstrip() for l in src[i:i + 3]]

    result = {
        "container_wall_seconds": round(wall, 1),
        "simulation": speed,
        "Q1_Q2_cli_attempts_during_run": attempts,
        "Q3_run_check_problems": problems,
        "Q4_rpc_thread_source": {"file": str(CONTROLLER_SRC.relative_to(ROOT)), "line": i + 1,
                                 "code": rpc_src},
        "priority_debug_job_lines": len(prio_lines),
        "priority_debug_factor_lines": len(factor_lines),
        "priority_debug_examples": factor_lines[:6] + prio_lines[:1],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
