"""Do the adaptive policies shorten waits for the class they favor, while they favor it?

Usage: .venv/bin/python -m evaluation.favored

For every adaptive matrix run, each job's wait [submit, start) is split over the policy's
states (from controller.jsonl; a state holds from the tick that set it to the next switch).
A job's exposure to state s is the share of its wait spent in s; a job that did not wait at
all takes the state at its submit time. Then, per state s in small / medium / large:
  favored  jobs of class s whose wait was mostly (>= 50%) in state s
  others   jobs of the other two classes whose wait was mostly in state s
Each group's mean wait is compared with the same jobs' mean wait in the C0 run of the same
workload (paired by job name). Writes results/phase3/favored.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from evaluation.metrics import job_table

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "config" / "evaluation.yaml"
OUT = ROOT / "results" / "phase3"
STATES = ["mix", "small", "medium", "large"]


def exposure(log: list[dict], submit: np.ndarray, start: np.ndarray) -> pd.DataFrame:
    """Share of each job's wait spent in each state (by submit-time state if it never waited)."""
    t = np.array([r["t_sim"] for r in log], dtype=float)
    st = [r["state"] for r in log]
    t = np.concatenate([[min(t[0], submit.min()) - 1], t, [max(t[-1], start.max()) + 1]])
    st = [log[0]["state_before"]] + st + [st[-1]]
    out = {}
    for s in STATES:
        dur = np.where(np.array(st[:-1]) == s, np.diff(t), 0.0)
        cum = np.concatenate([[0.0], np.cumsum(dur)])          # time in s up to each t
        out[s] = np.interp(start, t, cum) - np.interp(submit, t, cum)
    df = pd.DataFrame(out)
    waited = start - submit
    at_submit = np.array(st)[np.searchsorted(t, submit, side="right") - 1]
    for s in STATES:
        df[s] = np.where(waited > 0, df[s] / np.maximum(waited, 1), (at_submit == s).astype(float))
    return df


def analyse(cfg: dict) -> pd.DataFrame:
    rows = []
    root = ROOT / cfg["run_root"]
    for wl in cfg["workloads"]:
        base, _ = job_table(Path(cfg["run_root"]) / wl / "C0", wl)
        base = base.set_index("JobName")["wait_s"]
        for pol, d in cfg["policies"].items():
            if d["kind"] != "adaptive":
                continue
            run = Path(cfg["run_root"]) / wl / pol
            j, _ = job_table(run, wl)
            log = [json.loads(l) for l in (root / wl / pol / "policy" / "controller.jsonl").read_text().splitlines()]
            ex = exposure(log, j["Submit"].to_numpy(float), j["Start"].to_numpy(float))
            j = j.reset_index(drop=True).join(ex)
            j["c0_wait_s"] = j["JobName"].map(base)
            for s in ["small", "medium", "large"]:
                mostly = j[s] >= 0.5
                for group, mask in [("favored", mostly & (j["qos"] == s)),
                                    ("others", mostly & (j["qos"] != s))]:
                    g = j[mask]
                    rows.append({"workload": wl, "scenario": wl.split("-")[0], "policy": pol,
                                 "state": s, "group": group, "jobs": int(len(g)),
                                 "wait_h": float(g["wait_s"].mean() / 3600) if len(g) else np.nan,
                                 "c0_wait_h": float(g["c0_wait_s"].mean() / 3600) if len(g) else np.nan})
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Job-weighted means over the seeds, per scenario, policy, state and group."""
    df = df[df["jobs"] > 0].copy()
    df["w"] = df["wait_h"] * df["jobs"]
    df["w0"] = df["c0_wait_h"] * df["jobs"]
    g = df.groupby(["scenario", "policy", "state", "group"])[["jobs", "w", "w0"]].sum()
    g["wait_h"] = g["w"] / g["jobs"]
    g["c0_wait_h"] = g["w0"] / g["jobs"]
    g["change"] = g["wait_h"] / g["c0_wait_h"] - 1
    return g[["jobs", "wait_h", "c0_wait_h", "change"]].reset_index()


def main() -> None:
    cfg = yaml.safe_load(EVAL.read_text())
    per_run = analyse(cfg)
    summ = summarize(per_run)
    (OUT / "favored.json").write_text(json.dumps({
        "method": __doc__.split("\n\n", 1)[1].strip(),
        "summary": summ.round(4).to_dict("records"),
        "per_run": per_run.round(4).to_dict("records")}, indent=1) + "\n")
    pd.set_option("display.width", 160)
    print(summ.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
