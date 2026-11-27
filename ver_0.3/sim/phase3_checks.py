"""Tasks 3.2 (B1) and 3.3 (A3): checks of the image every Phase 3 run uses (slurmsim:v03).

Usage: .venv/bin/python -m sim.phase3_checks [--hours 24]

Runs in parallel:
  smoke  the smoke workload with the hook off. It must match results/phase2/smoke-hookoff (the
         Phase 2 image): the same node for every job, start times within 1 s.
  a3     the first --hours of K-s1 under the A3 policy (SPEC §8.3). At every tick the logged
         submission totals must lie between sacct's counts of jobs submitted before and at
         the tick time; the arrivals must be the totals' differences over the window; replay
         must be exact; the run check must pass.
  b1     the first --hours of K-s1 under B1 with DebugFlags=Priority (SPEC §8.2). The logged
         weights must be B1's; the job-size factor must grow with the CPU request and not
         depend on the time limit; the run check must pass.
Writes results/phase3/image_checks.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from controller.replay import replay
from sim.check_run import check
from sim.prepare_run import ROOT, prepare_run
from sim.priority_log import parse
from sim.runstats import load_acct

IMAGE = "slurmsim:v03"
OUT = ROOT / "results" / "phase3"
K = ROOT / "workloads" / "traces" / "K-s1"
K_START = 1_706_486_400
N_START = 1_716_163_200


def image_id() -> str:
    return subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def run(run_dir: Path) -> None:
    subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run_dir), "60"], check=True,
                   env={**os.environ, "SLURMSIM_IMAGE": IMAGE})


def k_slice(hours: float) -> Path:
    lines = [l for l in (K / "events.txt").read_text().splitlines()
             if l.strip() and int(float(l.split()[1])) < hours * 3600]
    path = OUT / "checks-input" / "k_slice_events.txt"      # inside an ignored folder
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def check_smoke(run_dir: Path, ref: Path) -> dict:
    new, old = load_acct(run_dir), load_acct(ref)
    m = new.merge(old, on="JobName", suffixes=("", "_ref"))
    res = {"jobs": int(len(new)), "matched": int(len(m)),
           "same_node": bool((m["NodeList"] == m["NodeList_ref"]).all()),
           "max_start_diff_s": int((m["Start"] - m["Start_ref"]).abs().max()),
           "run_problems": check(run_dir)}
    res["ok"] = bool(res["matched"] == res["jobs"] == len(old) and res["same_node"] and
                     res["max_start_diff_s"] <= 1 and not res["run_problems"])
    return res


def check_a3(run_dir: Path) -> dict:
    pol = ROOT / run_dir / "policy"
    params = json.loads((pol / "params.json").read_text())
    window = params["window_s"]
    log = [json.loads(l) for l in (pol / "controller.jsonl").read_text().splitlines()]
    acct = load_acct(run_dir)
    sub = {q: acct.loc[acct["QOS"] == q, "Submit"].sort_values().to_numpy()
           for q in ("small", "medium", "large")}
    totals_bad, arrivals_bad = [], []
    for i, r in enumerate(log):
        t = r["t_sim"]
        for q, s in sub.items():
            lo, hi = int((s < t).sum()), int((s <= t).sum())
            if not lo <= r["submitted"].get(q, 0) <= hi:
                totals_bad.append(f"t={t} {q}: logged {r['submitted'].get(q, 0)}, sacct {lo}..{hi}")
        base = {q: 0 for q in sub}
        for e in log[:i]:
            if e["t_sim"] <= t - window:
                base = e["submitted"]
        want = {q: r["submitted"].get(q, 0) - base.get(q, 0) for q in sub}
        if want != r["counts"]:
            arrivals_bad.append(f"t={t}: logged {r['counts']}, expected {want}")
    res = {"ticks": len(log), "switches": sum(r["switch"] for r in log),
           "states": sorted({r["state"] for r in log}),
           "arrivals_gated_ticks": sum("arrivals <" in r["reason"] for r in log),
           "totals_mismatches": totals_bad[:10], "arrivals_mismatches": arrivals_bad[:10],
           "replay_mismatches": replay(pol)[:10], "run_problems": check(run_dir)}
    res["ok"] = bool(log and not totals_bad and not arrivals_bad and not res["replay_mismatches"]
                     and not res["run_problems"])
    return res


def check_b1(run_dir: Path, jobs: pd.DataFrame) -> dict:
    pr = parse(ROOT / run_dir / "out" / "slurmctld.log", every_s=3600)
    pr = pr.merge(jobs[["cpus", "timelimit_min"]], left_on="job_id", right_index=True)
    want = {"Age_w": 1000, "Fairshare_w": 10000, "JobSize_w": 1000}
    weights_ok = {k: bool((pr[k] == v).all()) for k, v in want.items() if k in pr}
    # job size: one value per CPU request whatever the time limit, rising with the request
    by_cpu = pr.groupby("cpus").agg(f_min=("JobSize_f", "min"), f_max=("JobSize_f", "max"),
                                    limits=("timelimit_min", "nunique"), n=("job_id", "size"))
    const_per_cpu = bool(((by_cpu["f_max"] - by_cpu["f_min"]) < 1e-9).all())
    rising = bool(by_cpu["f_min"].is_monotonic_increasing and by_cpu["f_min"].is_unique)
    res = {"priority_blocks": int(len(pr)), "weights_ok": weights_ok,
           "jobsize_by_cpus": {int(c): float(f) for c, f in by_cpu["f_min"].items()},
           "cpu_values_with_several_limits": int((by_cpu["limits"] > 1).sum()),
           "jobsize_constant_per_cpu_request": const_per_cpu, "jobsize_rises_with_cpus": rising,
           "run_problems": check(run_dir)}
    res["ok"] = bool(len(pr) and len(weights_ok) == 3 and all(weights_ok.values()) and
                     const_per_cpu and rising and not res["run_problems"])
    return res


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hours", type=float, default=24)
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ev = k_slice(a.hours)
    smoke = Path("results/phase3/check-smoke-hookoff")
    prepare_run(smoke, ROOT / "sim/smoke/events.txt", ROOT / "sim/smoke/accounts.txt",
                ROOT / "config/priority/B0.conf", N_START)      # as the Phase 2 reference run
    a3 = Path("results/phase3/check-A3")
    prepare_run(a3, ev, K / "accounts.txt", ROOT / "config/priority/C0.conf", K_START,
                policy="adaptive", triggers=ROOT / "config/triggers_A3.yaml")
    b1 = Path("results/phase3/check-B1")
    prepare_run(b1, ev, K / "accounts.txt", ROOT / "config/priority/B1.conf", K_START,
                extra_conf=["DebugFlags=Priority"])
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(run, [smoke, a3, b1]))
    jobs = pd.read_parquet(K / "jobs.parquet").set_index("job_id")
    res = {"image": IMAGE, "image_id": image_id(), "hours": a.hours,
           "smoke_hook_off": check_smoke(smoke, Path("results/phase2/smoke-hookoff")),
           "a3_arrivals": check_a3(a3), "b1_priority": check_b1(b1, jobs)}
    res["ok"] = all(v["ok"] for v in res.values() if isinstance(v, dict))
    (OUT / "image_checks.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
