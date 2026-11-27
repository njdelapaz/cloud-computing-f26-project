"""Task 2.5: test the policy hook's apply step in the simulator (SPEC §7.6 item 2).

Usage: .venv/bin/python -m sim.apply_test [--hours 21] [--out results/phase2]

Run A (scheduled switches) on the first --hours of K-s1 with DebugFlags=Priority:
  C0 at start, then C1, C9 (missing: must be rejected), C2, C3, C0 at fixed times.
  Checks, for every applied switch at time T: Slurm's logged weights and every waiting job's
  QOS factor equal the new configuration's in the first priority calculation at or after T,
  and within 60 simulated seconds. C9 must be logged as an error and change nothing.
Run B (failing command) on the smoke workload with policy ticks whose command always fails:
  every tick logs the failure, no switch happens, and all jobs run as specified.
Writes OUT/apply_test.json (default results/phase2; task 3.3 reruns it into results/phase3
with the rebuilt image).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from sim.check_run import check
from sim.prepare_run import ROOT, prepare_run
from sim.priority_log import JOB, TS, WEIGHTED
from sim.weights import qos_factors

IMAGE = "slurmsim:v03"   # includes the policy hook (sim/patches/policy_hook.patch)
# (hours, config): during K-s1's first standing queue (about 40-160 jobs waiting at 16-19 h)
SWITCHES = [(16.5, "C1"), (17, "C9"), (17.5, "C2"), (18.5, "C3"), (19.5, "C0")]


def conf_weights(name: str) -> dict[str, float]:
    w = {}
    for line in (ROOT / "config" / "priority" / f"{name}.conf").read_text().splitlines():
        if line.startswith("PriorityWeight") and not line.startswith("PriorityWeightTRES"):
            k, v = line[len("PriorityWeight"):].split("=")
            w[k] = float(v)
    return w


def conf_qos(name: str) -> dict[str, int]:
    pri = {}
    for line in (ROOT / "config" / "priority" / f"{name}.qos").read_text().splitlines():
        if line and not line.startswith("#"):
            q, v = line.split()
            pri[q] = int(v)
    return pri


def run(run_dir: Path) -> None:
    subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run_dir), "60"], check=True,
                   env={**__import__("os").environ, "SLURMSIM_IMAGE": IMAGE})


def blocks_after(log: Path, t: pd.Timestamp, horizon_s: int = 600) -> pd.DataFrame:
    """Priority-calculation blocks (per job) logged from t to t + horizon."""
    rows, cur = [], {}
    end = t + pd.Timedelta(seconds=horizon_s)
    with open(log, errors="replace") as fh:
        for line in fh:
            if "_get_priority_internal" not in line:
                continue
            ts = pd.Timestamp(TS.match(line).group(1))
            if ts < t:
                continue
            if ts > end:
                break
            m = WEIGHTED.search(line)
            if m:
                if m.group(1) == "Age":
                    cur = {"t": ts}
                cur[m.group(1) + "_f"] = float(m.group(2))
                cur[m.group(1) + "_w"] = float(m.group(3))
                continue
            m = JOB.search(line)
            if m and cur:
                cur["job_id"] = int(m.group(1))
                rows.append(cur)
                cur = {}
    return pd.DataFrame(rows)


def check_switches(run_dir: Path, start: pd.Timestamp, jobs: pd.DataFrame) -> dict:  # noqa: ARG001
    log = ROOT / run_dir / "out" / "slurmctld.log"
    text = log.read_text(errors="replace")
    switch_re = re.compile(r"^\[(\S+)\] policy: switch (\S+) -> (\S+) \((\w+)\)", re.M)
    applied = [(pd.Timestamp(t), a, b, why) for t, a, b, why in switch_re.findall(text)]
    recomputed = [int(n) for n in re.findall(r"policy: recomputed priority of (\d+) pending jobs", text)]
    res = {"applied": [{"t": str(t), "from": a, "to": b, "why": why} for t, a, b, why in applied],
           "c9_rejected": bool(re.search(r"policy: keeping C1; cannot load C9", text)),
           "checks": []}
    for t, _, new, _ in applied:
        df = blocks_after(log, t)
        want_w = conf_weights(new)
        f = qos_factors(conf_qos(new))
        if df.empty:
            res["checks"].append({"to": new, "ok": False, "why": "no priority calculations"})
            continue
        # the switch recomputes every waiting job at T: take all blocks within 1 s of the first
        first_t = df["t"].min()
        first = df[df["t"] <= first_t + pd.Timedelta(seconds=1)].merge(
            jobs[["qos"]], left_on="job_id", right_index=True)
        w_ok = all((first[f"{k}_w"] == v).all() for k, v in want_w.items())
        # QOS factors are only computed when the QOS weight is non-zero
        q_ok = (want_w["QOS"] == 0) or bool(((first["QOS_f"] - first["qos"].map(f)).abs() < 1e-5).all())
        delay = (first_t - t).total_seconds()
        res["checks"].append({"to": new, "first_calc_after_s": delay, "jobs": int(len(first)),
                              "weights_ok": w_ok, "qos_factors_ok": q_ok,
                              "ok": bool(w_ok and q_ok and delay <= 60)})
    res["recomputed_per_switch"] = recomputed
    expected = [c for _, c in SWITCHES if c != "C9"]
    res["sequence_ok"] = [b for _, _, b, _ in applied] == expected
    res["ok"] = bool(res["sequence_ok"] and res["c9_rejected"] and
                     len(recomputed) == len(applied) and all(n > 0 for n in recomputed) and
                     all(c["ok"] for c in res["checks"]))
    return res


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hours", type=float, default=21)
    p.add_argument("--out", type=Path, default=Path("results/phase2"))
    a = p.parse_args()
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    src = ROOT / "workloads" / "traces" / "K-s1"
    jobs = pd.read_parquet(src / "jobs.parquet").set_index("job_id")

    # Run A: K slice plus scheduled switches
    lines = [l for l in (src / "events.txt").read_text().splitlines()
             if int(l.split()[1]) < a.hours * 3600]
    lines += [f"-dt {int(h * 3600)} -e set_priority_config | {c}" for h, c in SWITCHES]
    ev_a = out / "apply-input" / "apply_events.txt"   # inside an ignored folder
    ev_a.parent.mkdir(parents=True, exist_ok=True)
    ev_a.write_text("\n".join(lines) + "\n")
    run_a = a.out / "apply-A"
    prepare_run(run_a, ev_a, src / "accounts.txt", ROOT / "config" / "priority" / "C0.conf",
                1_706_486_400, extra_conf=["DebugFlags=Priority"], policy="scheduled")

    # Run B: smoke workload with a policy command that always fails
    run_b = a.out / "apply-B"
    prepare_run(run_b, ROOT / "sim" / "smoke" / "events.txt", ROOT / "sim" / "smoke" / "accounts.txt",
                ROOT / "config" / "priority" / "C0.conf", 1_716_163_200, policy="scheduled")
    bad = ROOT / run_b / "policy" / "fail.sh"
    bad.write_text("#!/bin/sh\necho 'injected failure' >&2\nexit 3\n")
    bad.chmod(0o755)
    with open(ROOT / run_b / "etc" / "sim.conf", "a") as fh:
        fh.write(f"PolicyTickSeconds = 300\nPolicyCommand = /work/{run_b}/policy/fail.sh\n")

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(run, [run_a, run_b]))

    res_a = check_switches(run_a, pd.Timestamp("2024-01-29"), jobs)
    res_a["run_problems"] = check(run_a)
    log_b = (ROOT / run_b / "out" / "slurmctld.log").read_text(errors="replace")
    res_b = {"ticks_failed": len(re.findall(r"policy: tick t=\d+ command failed", log_b)),
             "switches": len(re.findall(r"policy: switch", log_b)),
             "run_problems": check(run_b)}
    res_b["ok"] = res_b["ticks_failed"] > 0 and res_b["switches"] == 0 and not res_b["run_problems"]
    result = {"A_scheduled_switches": res_a, "B_failing_command": res_b,
              "ok": bool(res_a["ok"] and not res_a["run_problems"] and res_b["ok"])}
    result["image"] = IMAGE
    (out / "apply_test.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
