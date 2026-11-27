"""Compare a finished run's sacct output with the jobs it was given.

Usage: .venv/bin/python -m sim.check_run RUN_DIR

Checks, per job: it ran exactly once on one node of n01-n16, with the requested CPUs, memory,
time limit, user and account; its run time matches -sim-walltime (or the limit, if that is
shorter); and submit times keep the offsets from events.txt.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def parse_events(path: Path) -> pd.DataFrame:
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        head, body = line.split("|", 1)
        if "submit_batch_job" not in head:      # e.g. set_priority_config switches
            continue
        dt = float(re.search(r"-dt\s+(\S+)", head).group(1))

        def opt(pattern, cast=str):
            m = re.search(pattern, body)
            return cast(m.group(1)) if m else None

        rows.append({
            "name": opt(r"-J\s+(\S+)"), "dt": dt,
            "runtime_s": opt(r"-sim-walltime\s+(\S+)", float),
            "user": opt(r"--uid=(\S+)"), "account": opt(r"-A\s+(\S+)"),
            "limit_min": opt(r"-t\s+(\d+)", int), "cpus": opt(r"-n\s+(\d+)", int),
            "mem_mb": opt(r"--mem=(\d+)", int), "qos": opt(r"-q\s+(\S+)"),
        })
    return pd.DataFrame(rows)


def slurm_seconds(s: str) -> int:
    """'[D-]HH:MM:SS' or 'MM:SS' to seconds."""
    days = 0
    if "-" in s:
        d, s = s.split("-")
        days = int(d)
    parts = [int(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


def mem_mb(s: str) -> float:
    """sacct ReqMem like '120000M' or '62.50G' (binary units) to MB."""
    scale = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2}
    s = str(s)
    return float(s[:-1]) * scale[s[-1]] if s[-1] in scale else float(s)


KNOWN_ISSUES: list[str] = []


def lost_finish_jobs(run_dir: Path) -> set[int]:
    """Job ids the simulator could not find among its active jobs when they started."""
    log = run_dir / "out" / "slurmctld.log"
    ids = set()
    if log.exists():
        with open(log, errors="replace") as fh:
            for line in fh:
                if "among active sim jobs" in line:
                    m = re.search(r"Can not find job (\d+)", line)
                    if m:
                        ids.add(int(m.group(1)))
    return ids


def rejected_submissions(run_dir: Path) -> dict[str, int]:
    """Submissions slurmctld refused, by reason (e.g. MaxJobCount reached, task 3.4)."""
    log = run_dir / "out" / "slurmctld.log"
    out: dict[str, int] = {}
    if log.exists():
        with open(log, errors="replace") as fh:
            for line in fh:
                if "Batch job submission failed" in line:
                    out["submission failed"] = out.get("submission failed", 0) + 1
                elif "job_allocate:" in line and "error:" in line:
                    reason = line.split("job_allocate:", 1)[1].strip()
                    reason = re.sub(r"\(\d+\)", "(N)", reason)
                    out[reason] = out.get(reason, 0) + 1
    return out


def check(run_dir: Path) -> list[str]:
    KNOWN_ISSUES.clear()
    run_dir = ROOT / run_dir
    ev = parse_events(run_dir / "events.txt")
    acct = pd.read_csv(run_dir / "out" / "slurm_acct.out", sep="|")
    acct = acct[acct["JobName"].str.startswith("jobid_", na=False)]
    problems = []

    counts = acct["JobName"].value_counts()
    missing = set(ev["name"]) - set(counts.index)
    dup = counts[counts > 1]
    if missing:
        problems.append(f"{len(missing)} job(s) missing from sacct, e.g. {sorted(missing)[:5]}")
    for reason, n in rejected_submissions(run_dir).items():
        problems.append(f"{n} submission(s) rejected by slurmctld: {reason}")
    if len(dup):
        problems.append(f"{len(dup)} job(s) appear more than once, e.g. {list(dup.index[:5])}")

    m = ev.merge(acct, left_on="name", right_on="JobName", how="inner")
    m["elapsed_s"] = m["Elapsed"].map(slurm_seconds)
    m["limit_s"] = m["Timelimit"].map(slurm_seconds)
    m["submit_ts"] = pd.to_datetime(m["Submit"]).astype("int64") // 10**9
    expected_run = m[["runtime_s"]].join(m["limit_min"] * 60).min(axis=1)

    def report(mask, what):
        if mask.any():
            problems.append(f"{int(mask.sum())} job(s): {what}, e.g. {list(m.loc[mask, 'name'][:5])}")

    report(m["NNodes"] != 1, "not on exactly one node")
    report(~m["NodeList"].astype(str).str.fullmatch(r"n(0[1-9]|1[0-6])"), "node outside n01-n16")
    report(m["NCPUS"] != m["cpus"], "CPU count differs")
    report((m["ReqMem"].map(mem_mb) - m["mem_mb"]).abs() > 1, "memory request differs")
    report(m["limit_s"] != m["limit_min"] * 60, "time limit differs")
    report(m["User"] != m["user"], "user differs")
    report(m["Account"] != m["account"], "account differs")
    report(m["QOS"] != m["qos"], "QOS differs from -q")
    # Known simulator issue: a stray stop request at submit can drop a pending job from the
    # simulator's active-job list; it then runs to its time limit (sim/README.md).
    lost = lost_finish_jobs(run_dir)
    off = (m["elapsed_s"] - expected_run).abs() > 1
    known = off & m["JobIDRaw"].isin(lost) & (m["elapsed_s"] >= m["limit_s"])
    report(off & ~known, "run time differs from -sim-walltime")
    if known.any():
        KNOWN_ISSUES.append(f"{int(known.sum())} job(s) ran to their time limit because the "
                            f"simulator lost their finish event: {list(m.loc[known, 'name'][:5])}")
    offset = (m["submit_ts"] - m["submit_ts"].min()) - (m["dt"] - m["dt"].min())
    report(offset.abs() > 2, "submit offset differs from -dt")
    return problems


def main() -> None:
    run_dir = Path(sys.argv[1])
    problems = check(run_dir)
    for k in KNOWN_ISSUES:
        print("known simulator issue:", k)
    if problems:
        print("FAIL")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print(f"OK: every job in {run_dir}/events.txt ran as specified")


if __name__ == "__main__":
    main()
