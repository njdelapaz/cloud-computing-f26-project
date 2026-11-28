"""Tasks 4.2-4.3: check the warm start (SPEC §9.4) and the B1-based favored states (§9.2).

Usage: .venv/bin/python -m sim.phase4_checks [--part all|states|warm|carry|a4] [--hours 37]
                                             [--run DIR] [--analyse-only]

Part warm (results/phase4/warmstart_check.json). Two full runs under B1:
  warm     D-nat-s1 as built: warm start, 12 h lead-in, two measured days, 24 h lead-out
  control  the same measured days and lead-out after a 48 h natural lead-in and no warm start
           (the cluster then fills on its own: 0.93 of its busy-period load after 48 h)
  The warm start passes if, over the measured days, the allocated CPU and memory shares
  average within 0.05 of the control's (both over all 48 h and over the first 6 h), and the
  measured jobs' mean wait is within 30% of the control's (the Phase 3 noise bands were
  22-31%). The waiting queue is reported, with the B1 run of N-s1, days 2-4, for reference.
  Result (2026-11-27): failed. The warm run was the busier one (memory 0.90 against 0.81,
  mean wait 29 against 6 min): the control's lead-in carried less load into the measured
  days than a settled cluster does, by chance. A single control run is too noisy a yardstick,
  so the warm start is judged by part carry instead (decided on request).

Part carry (results/phase4/warmstart_carry.json; no simulation). For seeds 1-8, the memory and
  CPU load carried into the measured days by the jobs submitted before them (warm start and
  lead-in), counting every job as running from its submission: the warm start with the
  12 h lead-in, a 48 h lead-in, and a 7-day lead-in, which stands for a settled cluster
  (0.997-1.0 of its busy-period load). Reported: each variant's mean and spread over seeds.

Part a4 (task 4.4; results/phase4/a4_slice_check.json). The first 36 h of D-alt-s1 under A4
  (config/triggers_A4.yaml, tight targets so that spells occur, 60-min stabilization). Passes
  when controller.summarize does: every tick logged on both sides, every switch applied,
  replay exact, run check clean. Its spell statistics are reported.

Part states (results/phase4/states_check.json). One run: the first --hours of D-nat-s1,
starting in B1, with DebugFlags=Priority and
scheduled switches B1 -> B1L -> B1 -> B1M -> B1, twice (SWITCHES). Checks:
  static    the run's slurm.conf holds B1's static keys, and in every state the job-size factor
            depends on the CPU request only (B1 has no SMALL_RELATIVE_TO_TIME)
  switches  every switch is applied, in order; the first priority calculation after it, within
            60 s, has the new state's weights and each waiting job's QOS factor for its class
  states    in every 10-minute snapshot the logged weights are the active state's; while B1M
            or B1L is active, favored jobs carry 5000 QOS points and every other job 0, so
            within a class the order is B1's
  order     in those snapshots, for every pair of a favored job and a job of another class
            whose priorities without the boost differ by less than 5000, the favored job is
            ahead. The share of all cross-class pairs with the favored job ahead is reported
  queue     every favored spell has a snapshot with at least 20 waiting jobs of both kinds
  run       sim/check_run.py passes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import yaml

from sim.apply_test import blocks_after, conf_qos, conf_weights
from sim.check_run import check
from sim.prepare_run import ROOT, prepare_run
from sim.priority_log import parse
from sim.runstats import load_acct
from sim.weights import qos_factors
from workloads import build

IMAGE = "slurmsim:v03"
OUT = ROOT / "results" / "phase4"
D = ROOT / "workloads" / "traces" / "D-nat-s1"
D_START = 1_720_353_600          # 2024-07-07 12:00 UTC: the measured days start on a Monday
RUN = Path("results/phase4/check-states")
# (hours from the start, state). The measured days start at 12 h. The spells sit where the
# warm run of D-nat-s1 under B1 has a standing queue with medium and large jobs in it
# (27-36 h: 160-250 waiting, 9-78 large, 49-124 medium); at 14-22 h, the first placement,
# the queue drained overnight to 5-7 jobs.
SWITCHES = [(27, "B1L"), (28, "B1"), (29, "B1M"), (30, "B1"),
            (32, "B1L"), (33, "B1"), (34, "B1M"), (35, "B1")]
FAVORED = {"B1L": "large", "B1M": "medium"}
STATIC = {"PriorityDecayHalfLife": "14-0", "PriorityMaxAge": "14-0", "PriorityFavorSmall": "NO",
          "PriorityWeightPartition": "1000"}
SNAP_S = 600
WARM = Path("results/phase4/check-warm")
CTL = Path("results/phase4/check-warm-control")
CTL_LEAD_H = 48
DAY = 86_400
A4_RUN = Path("results/phase4/check-a4")
A4_WL = ROOT / "workloads" / "traces" / "D-alt-s1"
A4_HOURS = 36
A4_OVERRIDES = {"target_level": "tight", "stable_s": 3600}


def prepare_a4() -> None:
    lines = [l for l in (A4_WL / "events.txt").read_text().splitlines()
             if l.strip() and int(float(l.split()[1])) < A4_HOURS * 3600]
    ev = OUT / "checks-input" / "a4_events.txt"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("\n".join(lines) + "\n")
    prepare_run(A4_RUN, ev, A4_WL / "accounts.txt", ROOT / "config/priority/B1.conf", D_START,
                policy="adaptive", triggers=ROOT / "config/triggers_A4.yaml",
                trigger_overrides=A4_OVERRIDES)


def check_a4() -> dict:
    from controller.summarize import summarize
    res = summarize(A4_RUN, window_h=A4_HOURS)
    a4 = res.get("a4", {})
    return {"workload": "D-alt-s1", "hours": A4_HOURS, "overrides": A4_OVERRIDES,
            **{k: res[k] for k in ("ticks_controller", "ticks_simulator", "command_failures",
                                   "switches_asked", "switches_applied", "applied_matches_asked",
                                   "replay_mismatches", "run_problems", "command_ms_median")},
            "spells_by_state": a4.get("by_state"), "time_share_by_phase": a4.get("time_share_by_phase"),
            "spells": a4.get("spells"), "ok": res["ok"]}


def run_sim(run_dir: Path) -> None:
    subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run_dir), "60"], check=True,
                   env={**os.environ, "SLURMSIM_IMAGE": IMAGE})


# --------------------------------------------------------------------------- warm start

CARRY_SEEDS = range(1, 9)
CARRY_VARIANTS = {"warm start + 12 h lead-in": {"warm_start": True, "leadin_hours": 12},
                  "48 h lead-in": {"warm_start": False, "leadin_hours": 48},
                  "7-day lead-in": {"warm_start": False, "leadin_hours": 168}}


def carried(kept: pd.DataFrame, lo: int, step: int = 1800) -> dict:
    """Load the jobs submitted before the measured days carry into them, every job counted as
    running from its submission for its run time."""
    pre = kept[kept["segment"].isin(["warmstart", "leadin"])]
    s = pre["submit_s"].to_numpy()
    e = s + pre["runtime_s"].to_numpy()
    ts = np.arange(lo, lo + 2 * DAY, step)
    on = [(s <= t) & (e > t) for t in ts]
    mem = np.array([pre["mem_mb"].to_numpy()[m].sum() for m in on]) / 2_048_000
    cpu = np.array([pre["cpus"].to_numpy()[m].sum() for m in on]) / 512
    first = ts < lo + 6 * 3600
    return {"mem": float(mem.mean()), "cpu": float(cpu.mean()),
            "mem_first_6h": float(mem[first].mean()), "cpu_first_6h": float(cpu[first].mean())}


def check_carry() -> dict:
    cfg = yaml.safe_load(build.CONFIG.read_text())
    jobs = build.load_jobs()
    rows = []
    for seed in CARRY_SEEDS:
        for name, ov in CARRY_VARIANTS.items():
            kept, info = build.day_workload(jobs, "D", "nat", {**cfg["day"]["D"], **ov}, seed,
                                            cfg["target_rho"])
            rows.append({"seed": seed, "variant": name,
                         **carried(kept, info["measured_window_s"][0])})
    df = pd.DataFrame(rows)
    summary = {v: {k: {"mean": float(g[k].mean()), "sd": float(g[k].std())}
                   for k in ("mem", "cpu", "mem_first_6h", "cpu_first_6h")}
               for v, g in df.groupby("variant")}
    ref, warm = summary["7-day lead-in"], summary["warm start + 12 h lead-in"]
    return {"seeds": list(CARRY_SEEDS), "per_seed": rows, "summary": summary,
            "warm_minus_7day_mean": {k: warm[k]["mean"] - ref[k]["mean"] for k in ref}}

def build_control() -> Path:
    """D-nat-s1 with a 48 h natural lead-in and no warm start (same measured days, lead-out)."""
    cfg = yaml.safe_load(build.CONFIG.read_text())
    ctl = {**cfg["day"]["D"], "leadin_hours": CTL_LEAD_H, "warm_start": False}
    kept, info = build.day_workload(build.load_jobs(), "D", "nat", ctl, 1, cfg["target_rho"])
    real, build.TRACES = build.TRACES, OUT / "checks-input"       # an ignored folder
    try:
        build.write_scenario(build.finalize(kept), "D48-nat", 1, cfg["target_rho"], info, None,
                             ctl["measured_days"] * DAY)
    finally:
        build.TRACES = real
    return OUT / "checks-input" / "D48-nat-s1"


def prepare_warm() -> None:
    ctl = build_control()
    prepare_run(WARM, D / "events.txt", D / "accounts.txt", ROOT / "config/priority/B1.conf",
                D_START)
    # the control's measured days start at the same calendar time as the warm run's
    prepare_run(CTL, ctl / "events.txt", ctl / "accounts.txt", ROOT / "config/priority/B1.conf",
                D_START + 12 * 3600 - CTL_LEAD_H * 3600)


def series(run: Path, jobs_path: Path, t_start: int, lo: int, hi: int, step: int = SNAP_S
           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Allocated CPU and memory shares and jobs waiting every `step` s over [lo, hi), and
    the accounting joined to the workload's jobs."""
    a = load_acct(run)
    j = pd.read_parquet(jobs_path).set_index("name")
    a = a.join(j[["segment", "src_jobid", "submit_s", "cpus", "mem_mb"]].add_prefix("w_"),
               on="JobName")
    s, st, e = (a["Submit"] - t_start).to_numpy(), (a["Start"] - t_start).to_numpy(), \
        (a["End"] - t_start).to_numpy()
    cpu, mem = a["w_cpus"].to_numpy(float), a["w_mem_mb"].to_numpy(float)
    rows = []
    for t in range(lo, hi, step):
        run_ = (st <= t) & (e > t)
        rows.append({"t_h": (t - lo) / 3600, "cpu": cpu[run_].sum() / 512,
                     "mem": mem[run_].sum() / 2_048_000,
                     "waiting": int(((s <= t) & (st > t)).sum())})
    return pd.DataFrame(rows), a


def summarize_series(ts: pd.DataFrame) -> dict:
    first = ts[ts["t_h"] < 6]
    return {"cpu_mean": float(ts["cpu"].mean()), "mem_mean": float(ts["mem"].mean()),
            "cpu_first_6h": float(first["cpu"].mean()), "mem_first_6h": float(first["mem"].mean()),
            "waiting_median": float(ts["waiting"].median()),
            "share_of_samples_with_20_waiting": float((ts["waiting"] >= 20).mean())}


def check_warm() -> dict:
    ctl_dir = OUT / "checks-input" / "D48-nat-s1"
    runs = {"warm": (WARM, D, D_START, 12 * 3600),
            "control": (CTL, ctl_dir, D_START + 12 * 3600 - CTL_LEAD_H * 3600, CTL_LEAD_H * 3600)}
    out, waits = {}, {}
    for name, (run, wl, t_start, lo) in runs.items():
        ts, a = series(run, wl / "jobs.parquet", t_start, lo, lo + 2 * DAY)
        meas = a[~a["w_segment"].isin(["warmstart", "leadin", "leadout"])].copy()
        meas["key"] = list(zip(meas["w_src_jobid"], meas["w_submit_s"] - lo))
        meas["wait_s"] = meas["Start"] - meas["Submit"]
        waits[name] = meas.set_index("key")["wait_s"]
        out[name] = {**summarize_series(ts), "measured_jobs": int(len(meas)),
                     "wait_mean_min": float(meas["wait_s"].mean() / 60),
                     "run_problems": check(run)}
    both = pd.concat(waits, axis=1).dropna()
    ref_ts, _ = series(Path("results/phase3/runs/N-s1/B1"),
                       ROOT / "workloads/traces/N-s1/jobs.parquet", 1_716_163_200, 2 * DAY, 5 * DAY)
    w, c = out["warm"], out["control"]
    diffs = {k: abs(w[k] - c[k]) for k in ("cpu_mean", "mem_mean", "cpu_first_6h", "mem_first_6h")}
    ratio = w["wait_mean_min"] / c["wait_mean_min"] if c["wait_mean_min"] else float("nan")
    res = {**out, "occupancy_differences": diffs,
           "wait_mean_ratio_warm_to_control": ratio,
           "paired_jobs": int(len(both)),
           "paired_wait_mean_min": {k: float(both[k].mean() / 60) for k in both},
           "reference_N_s1_B1_days_2_to_4": summarize_series(ref_ts),
           "ok": bool(all(v <= 0.05 for v in diffs.values()) and 1 / 1.3 <= ratio <= 1.3
                      and not w["run_problems"] and not c["run_problems"])}
    return res


def image_id() -> str:
    return subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def prepare(hours: float) -> None:
    lines = [l for l in (D / "events.txt").read_text().splitlines()
             if l.strip() and int(float(l.split()[1])) < hours * 3600]
    lines += [f"-dt {int(h * 3600)} -e set_priority_config | {c}" for h, c in SWITCHES]
    ev = OUT / "checks-input" / "states_events.txt"          # inside an ignored folder
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text("\n".join(lines) + "\n")
    prepare_run(RUN, ev, D / "accounts.txt", ROOT / "config/priority/B1.conf", D_START,
                extra_conf=["DebugFlags=Priority"], policy="scheduled")


def applied_switches(log_text: str) -> list[tuple[pd.Timestamp, str, str]]:
    rx = re.compile(r"^\[(\S+)\] policy: switch (\S+) -> (\S+) \((\w+)\)", re.M)
    return [(pd.Timestamp(t), a, b) for t, a, b, _ in rx.findall(log_text)]


def check_static(conf: str, pr: pd.DataFrame) -> dict:
    lines = conf.splitlines()
    keys = {k: [l for l in lines if l.startswith(f"{k}=")] for k in STATIC}
    keys_ok = all(v == [f"{k}={STATIC[k]}"] for k, v in keys.items())
    no_flags = not any(l.startswith("PriorityFlags=") for l in lines)
    by = pr.groupby(["state", "cpus"])["JobSize_f"].agg(["min", "max"])
    const = bool(((by["max"] - by["min"]) < 1e-9).all())
    rising = all(g["min"].is_monotonic_increasing for _, g in by.groupby(level="state"))
    return {"static_keys_ok": keys_ok, "no_priority_flags": no_flags,
            "jobsize_depends_on_cpus_only": const, "jobsize_rises_with_cpus": bool(rising),
            "ok": bool(keys_ok and no_flags and const and rising)}


def weights_match(df: pd.DataFrame, want: dict[str, float]) -> bool:
    """Logged weights equal want. Slurm logs no line for a factor whose weight is 0."""
    for k, v in want.items():
        col = f"{k}_w"
        if v == 0:
            if col in df and not (df[col].fillna(0) == 0).all():
                return False
        elif col not in df or not (df[col] == v).all():
            return False
    return True


def check_switches(log: Path, applied, jobs: pd.DataFrame) -> dict:
    out = []
    for t, _, new in applied:
        df = blocks_after(log, t)
        if df.empty:
            out.append({"to": new, "ok": False, "why": "no priority calculations"})
            continue
        first_t = df["t"].min()
        first = df[df["t"] <= first_t + pd.Timedelta(seconds=1)].merge(
            jobs[["qos"]], left_on="job_id", right_index=True)
        want_w = conf_weights(new)
        w_ok = weights_match(first, want_w)
        f = qos_factors(conf_qos(new))
        q_ok = (want_w["QOS"] == 0) or bool(((first["QOS_f"] - first["qos"].map(f)).abs() < 1e-6).all())
        delay = (first_t - t).total_seconds()
        out.append({"t": str(t), "to": new, "first_calc_after_s": round(delay, 3),
                    "jobs": int(len(first)), "weights_ok": bool(w_ok), "qos_factors_ok": q_ok,
                    "ok": bool(w_ok and q_ok and 0 <= delay <= 60)})
    want = [c for _, c in SWITCHES]
    return {"sequence": [b for _, _, b in applied], "sequence_ok": [b for _, _, b in applied] == want,
            "checks": out, "ok": bool([b for _, _, b in applied] == want and all(c["ok"] for c in out))}


def state_at(applied, t: pd.Timestamp) -> str | None:
    """Active state at t, or None in the minute after a switch (mixed calculations)."""
    state = "B1"
    for ts, _, new in applied:
        if ts <= t:
            state = new
        if ts <= t + pd.Timedelta(seconds=60) and ts > t - pd.Timedelta(seconds=60):
            return None
    return state


def check_states_and_order(pr: pd.DataFrame, min_jobs: int = 20) -> dict:
    snaps, spells_ok, pairs_total, pairs_ahead, viol = [], {}, 0, 0, []
    for snap, g in pr.groupby("snapshot"):
        state = g["state"].iloc[0]
        w_ok = weights_match(g, conf_weights(state))
        rec = {"t": str(snap), "state": state, "waiting": int(len(g)), "weights_ok": bool(w_ok)}
        if state in FAVORED:
            fav = FAVORED[state]
            is_f = (g["qos"] == fav).to_numpy()
            qos_v = g["QOS_v"].to_numpy()
            boost_ok = bool(np.allclose(qos_v[is_f], 5000) and np.allclose(qos_v[~is_f], 0))
            base = g["priority"].to_numpy() - qos_v
            pf, po = g["priority"].to_numpy()[is_f], g["priority"].to_numpy()[~is_f]
            bf, bo = base[is_f], base[~is_f]
            ahead = pf[:, None] > po[None, :]
            close = (bo[None, :] - bf[:, None]) < 5000 - 1       # boost exceeds the B1 gap
            bad = int((close & ~ahead).sum())
            rec.update({"favored": int(is_f.sum()), "others": int((~is_f).sum()),
                        "boost_ok": boost_ok, "pairs": int(ahead.size),
                        "pairs_within_boost": int(close.sum()), "violations": bad,
                        "favored_ahead_share": float(ahead.mean()) if ahead.size else None})
            pairs_total += int(ahead.size)
            pairs_ahead += int(ahead.sum())
            if bad or not boost_ok:
                viol.append(rec)
            spell = spells_ok.setdefault(int(g["spell"].iloc[0]),
                                         {"state": state, "snapshots": 0, "with_queue": 0})
            spell["snapshots"] += 1
            spell["with_queue"] += int(len(g) >= min_jobs and is_f.any() and (~is_f).any())
        snaps.append(rec)
    weights_ok = all(s["weights_ok"] for s in snaps)
    queue_ok = bool(spells_ok) and all(s["with_queue"] > 0 for s in spells_ok.values())
    return {"snapshots": len(snaps), "weights_ok_all_snapshots": weights_ok,
            "spells": spells_ok, "queue_ok": queue_ok,
            "cross_class_pairs": pairs_total,
            "favored_ahead_share": pairs_ahead / pairs_total if pairs_total else None,
            "violations": viol[:10],
            "ok": bool(weights_ok and queue_ok and not viol and pairs_total > 0),
            "per_snapshot": [s for s in snaps if s["state"] in FAVORED]}


def analyse() -> dict:
    log = ROOT / RUN / "out" / "slurmctld.log"
    text = log.read_text(errors="replace")
    applied = applied_switches(text)
    jobs = pd.read_parquet(D / "jobs.parquet").set_index("job_id")
    pr = parse(log, every_s=SNAP_S)
    pr = pr.merge(jobs[["qos", "cpus", "timelimit_min"]], left_on="job_id", right_index=True)
    pr["state"] = [state_at(applied, t) for t in pr["snapshot"]]
    pr = pr[pr["state"].notna()].copy()
    # spell number: count of switches into a favored state up to the snapshot
    starts = [ts for ts, _, new in applied if new in FAVORED]
    pr["spell"] = [sum(ts <= t for ts in starts) for t in pr["snapshot"]]
    res = {"image": IMAGE, "image_id": image_id(), "workload": "D-nat-s1",
           "static": check_static((ROOT / RUN / "etc/slurm.conf").read_text(), pr),
           "switches": check_switches(log, applied, jobs),
           "states_and_order": check_states_and_order(pr),
           "run_problems": check(RUN)}
    res["ok"] = bool(res["static"]["ok"] and res["switches"]["ok"] and
                     res["states_and_order"]["ok"] and not res["run_problems"])
    return res


def main() -> None:
    global RUN
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--part", choices=["all", "states", "warm", "carry", "a4"], default="all")
    p.add_argument("--hours", type=float, default=37)
    p.add_argument("--run", type=Path, default=RUN, help="run folder of the states part")
    p.add_argument("--analyse-only", action="store_true", help="reuse the existing runs")
    a = p.parse_args()
    RUN = a.run
    OUT.mkdir(parents=True, exist_ok=True)
    parts = ["warm", "states", "carry", "a4"] if a.part == "all" else [a.part]
    if not a.analyse_only:
        runs = []
        if "warm" in parts:
            prepare_warm()
            runs += [WARM, CTL]
        if "states" in parts:
            prepare(a.hours)
            runs.append(RUN)
        if "a4" in parts:
            prepare_a4()
            runs.append(A4_RUN)
        if runs:
            with ThreadPoolExecutor(max_workers=len(runs)) as ex:
                list(ex.map(run_sim, runs))
    summary = {}
    if "warm" in parts:
        res = check_warm()
        (OUT / "warmstart_check.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
        summary["warm"] = res["ok"]
    if "a4" in parts:
        res = check_a4()
        (OUT / "a4_slice_check.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
        summary["a4"] = {k: res[k] for k in ("ticks_controller", "switches_applied",
                                             "replay_mismatches", "ok")}
    if "carry" in parts:
        res = check_carry()
        (OUT / "warmstart_carry.json").write_text(json.dumps(res, indent=2) + "\n")
        summary["carry"] = {k: round(v, 4) for k, v in res["warm_minus_7day_mean"].items()}
    if "states" in parts:
        res = {"hours": a.hours, **analyse()}
        (OUT / "states_check.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
        summary["states"] = {k: (v["ok"] if isinstance(v, dict) and "ok" in v else v)
                             for k, v in res.items() if k != "image_id"}
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
