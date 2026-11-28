"""Tasks 3.2, 4.5: the metrics of one finished run (SPEC §8.5, §9.6), and the table of all runs.

Usage:
  .venv/bin/python -m evaluation.metrics [--config CFG] RUN_DIR WORKLOAD   one run, as JSON
  .venv/bin/python -m evaluation.metrics [--config CFG] --collect          every run under
                                                         run_root -> the config's metrics_out

Inputs: the run's sacct output, the workload's jobs.parquet (class, size QOS, account,
segment) and, for adaptive runs, the summary controller.summarize writes next to the run.
Settings come from the config's metrics block (config/evaluation.yaml by default; Phase 4:
config/evaluation_setup.yaml). With measured_only, only the jobs of the measured days count
(not the warm start, lead-in or lead-out); with window: measured, cluster state is taken over
the workload manifest's measured_window_s.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from sim.runstats import cluster_balance, load_acct

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "config" / "evaluation.yaml"
OUT = ROOT / "results" / "phase3"
SIZES = ("small", "medium", "large")
CLASSES = ("sm-short", "sm-med", "sm-long", "med-short", "med-med", "med-long",
           "lg-short", "lg-med", "lg-long")


NOT_MEASURED = ("warmstart", "leadin", "leadout")


def settings(path: Path | None = None) -> dict:
    return yaml.safe_load((ROOT / (path or EVAL)).read_text())


def job_table(run_dir: Path, workload: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """sacct rows joined with the workload's jobs; also returns the raw sacct table."""
    acct = load_acct(run_dir)
    jobs = pd.read_parquet(ROOT / "workloads" / "traces" / workload / "jobs.parquet")
    j = acct.merge(jobs[["name", "class", "qos", "account", "segment"]], left_on="JobName",
                   right_on="name", how="left", suffixes=("_sacct", ""))
    if j["class"].isna().any():
        raise ValueError(f"{run_dir}: {int(j['class'].isna().sum())} jobs not in {workload}")
    j["run_s"] = j["End"] - j["Start"]
    j["day"] = ((j["Submit"] - j["Submit"].min()) // 86400).astype(int)
    return j, acct


def bounded_slowdown(j: pd.DataFrame, tau_s: float) -> pd.Series:
    return (j["wait_s"] + j["run_s"]) / np.maximum(j["run_s"], tau_s)


def jain(x: pd.Series) -> float:
    """Jain's index (sum x)^2 / (n * sum x^2): 1 when every value is equal."""
    x = x.astype(float)
    return float(x.sum() ** 2 / (len(x) * (x ** 2).sum())) if len(x) and (x ** 2).sum() else float("nan")


def service(j: pd.DataFrame, tau_s: float) -> dict:
    w = j["wait_s"] / 60
    out = {"jobs": int(len(j)), "wait_mean_min": float(w.mean()),
           "wait_median_min": float(w.median()), "wait_p95_min": float(w.quantile(.95)),
           "bsld_mean": float(bounded_slowdown(j, tau_s).mean())}
    for q in SIZES:
        wq = w[j["qos"] == q]
        out[f"wait_mean_min_{q}"] = float(wq.mean()) if len(wq) else float("nan")
        out[f"wait_p95_min_{q}"] = float(wq.quantile(.95)) if len(wq) else float("nan")
    for c in CLASSES:
        wc = w[j["class"] == c]
        out[f"wait_mean_min_{c}"] = float(wc.mean()) if len(wc) else float("nan")
    return out


def fairness(j: pd.DataFrame, tau_s: float, min_jobs: int) -> dict:
    per = j.assign(b=bounded_slowdown(j, tau_s)).groupby("account")["b"].agg(["mean", "size"])
    per = per[per["size"] >= min_jobs]
    return {"jain_bsld": jain(per["mean"]), "jain_accounts": int(len(per))}


def cluster(acct: pd.DataFrame, window_s: int, sample_s: int, from_s: int = 0) -> dict:
    bal = cluster_balance(acct, until_s=window_s, step=sample_s, from_s=from_s)
    return {"util": bal["util"], "util_cpu": bal["cpu"], "util_mem": bal["mem"],
            "node_sigma": bal["sigma_u"],
            "makespan_h": float((acct["End"].max() - acct["Submit"].min()) / 3600)}


def segments(j: pd.DataFrame) -> dict:
    """Mean wait by submission day, and for Y the burst and background jobs (SPEC §8.5)."""
    w = j["wait_s"] / 60
    out = {f"wait_mean_min_day{d}": float(w[j["day"] == d].mean()) for d in sorted(j["day"].unique())}
    seg = j["segment"].fillna("")
    if seg.str.startswith("burst").any():
        out["wait_mean_min_base"] = float(w[seg == "base"].mean())
        out["wait_mean_min_burst"] = float(w[seg.str.startswith("burst")].mean())
    return out


def adaptation(run_dir: Path) -> dict:
    s = ROOT / run_dir.parent / f"{run_dir.name}_summary.json"
    if not s.exists():
        return {}
    r = json.loads(s.read_text())
    wk = r["submission_week"]
    out = {"switches_per_day": wk["switches_per_day"], "reversals_2h": wk["reversals_within_2h"],
           "switches": wk["switches"], "stays_at_minimum": wk["switches_at_minimum_dwell"],
           "command_ms_median": r["command_ms_median"]}
    out.update({f"time_share_{k}": v for k, v in wk["time_share_by_state"].items()})
    for st, g in r.get("a4", {}).get("by_state", {}).items():         # A4 spells (SPEC §9.6)
        out[f"spells_{st}"] = g["spells"]
        out[f"spells_per_day_{st}"] = g["per_day"]
        out[f"spells_first_chance_{st}"] = g["started_at_first_chance"]
    for ph, v in r.get("a4", {}).get("time_share_by_phase", {}).items():
        out[f"phase_share_{ph}"] = v
    return out


def a4_measured(run_dir: Path, origin: int, lo: int, hi: int) -> dict:
    """A4 spells over the measured window [origin + lo, origin + hi) (SPEC §9.6): spells
    started in it per state, and the share of the window each class was favored."""
    log_path = ROOT / run_dir / "policy" / "controller.jsonl"
    log = [json.loads(l) for l in log_path.read_text().splitlines()]
    if not log or log[0].get("signal") != "wait_target":
        return {}
    a, b = origin + lo, origin + hi
    out = {}
    active = {}
    for r, nxt in zip(log, log[1:] + [None]):
        t0, t1 = r["t_sim"], (nxt["t_sim"] if nxt else r["t_sim"])
        if r["phase"] == "active":
            overlap = max(0, min(t1, b) - max(t0, a))
            active[r["state"]] = active.get(r["state"], 0) + overlap
    for st in ("medium", "large"):
        out[f"m_spells_{st}"] = sum(1 for r in log if r["switch"] and r["state"] == st
                                    and a <= r["t_sim"] < b)
        out[f"m_active_share_{st}"] = active.get(st, 0) / (b - a)
    out["m_active_share"] = sum(active.values()) / (b - a)
    return out


def run_metrics(run_dir: Path, workload: str, full: dict | None = None) -> dict:
    cfg = (full or settings())["metrics"]
    j, acct = job_table(run_dir, workload)
    if cfg.get("measured_only"):
        j = j[~j["segment"].isin(NOT_MEASURED)].copy()
        j["day"] = ((j["Submit"] - j["Submit"].min()) // 86400).astype(int)
    if cfg.get("window") == "measured":
        m = json.loads((ROOT / "workloads" / "traces" / workload / "manifest.json").read_text())
        lo, hi = m["measured_window_s"]
        clu = cluster(acct, hi, cfg["sample_s"], from_s=lo)
    else:
        clu = cluster(acct, cfg["window_s"], cfg["sample_s"])
    row = {}
    row.update(service(j, cfg["bsld_tau_s"]))
    row.update(fairness(j, cfg["bsld_tau_s"], cfg["jain_min_jobs"]))
    row.update(clu)
    row.update(segments(j))
    row.update(adaptation(run_dir))
    if cfg.get("window") == "measured" and (ROOT / run_dir / "policy" / "controller.jsonl").exists():
        row.update(a4_measured(run_dir, int(acct["Submit"].min()), lo, hi))
    return row


def collect(cfg: dict | None = None) -> pd.DataFrame:
    """One row per finished run under run_root (each run folder has done.json from sim.batch)."""
    cfg = cfg or settings()
    rows = []
    for done in sorted((ROOT / cfg["run_root"]).glob("*/*/done.json")):
        if ".attempt" in done.parent.name:          # a failed first attempt kept by sim.batch
            continue
        d = json.loads(done.read_text())
        if d["policy"] not in cfg["policies"]:      # runs of policies dropped from the comparison
            continue
        if d["workload"] not in cfg["workloads"]:   # workloads dropped from the evaluation
            continue
        row = {k: d[k] for k in ("workload", "scenario", "seed", "policy", "role", "ok",
                                 "attempt", "image_id", "real_s")}
        if d["ok"]:
            row.update(run_metrics(done.parent.relative_to(ROOT), d["workload"], cfg))
        rows.append(row)
    df = pd.DataFrame(rows)
    out = ROOT / cfg.get("metrics_out", "results/phase3/metrics.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, float_format="%.6g")
    return df


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=EVAL)
    p.add_argument("run_dir", type=Path, nargs="?")
    p.add_argument("workload", nargs="?")
    p.add_argument("--collect", action="store_true")
    a = p.parse_args()
    cfg = settings(a.config)
    if a.collect:
        df = collect(cfg)
        print(f"{len(df)} runs -> {cfg.get('metrics_out', 'results/phase3/metrics.csv')}")
        return 0
    if not (a.run_dir and a.workload):
        p.error("RUN_DIR and WORKLOAD, or --collect")
    print(json.dumps(run_metrics(a.run_dir, a.workload, cfg), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
