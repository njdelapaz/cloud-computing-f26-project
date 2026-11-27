"""Task 3.2 / 3.6: noise bands, the bar S*, paired differences and the SPEC §8.6 answer.

Usage: .venv/bin/python -m evaluation.compare [--metrics results/phase3/metrics.csv]

Reads the per-run table from evaluation.metrics and config/evaluation.yaml; writes
results/phase3/comparison.json.

  noise band b_s   for scenario s, the larger relative range (max - min) / mean of the primary
                   metric over the repeat runs of each repeat policy on the scenario's repeat
                   workload (pilot, matrix and extra run).
  S*               the static policy with the lowest primary metric averaged over all
                   evaluation workloads (matrix runs).
  effective on s   on every seed: primary < S*'s by more than b_s * S*'s, and the guards hold.
  answer           yes / partly / no, as in SPEC §8.6.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "config" / "evaluation.yaml"
OUT = ROOT / "results" / "phase3"
SIZES = ("small", "medium", "large")


def noise_bands(df: pd.DataFrame, cfg: dict) -> dict:
    primary = cfg["primary"]
    out = {}
    for wl in cfg["repeats"]["workloads"]:
        scen = wl.split("-")[0]
        sets = {}
        for pol in cfg["repeats"]["policies"]:
            v = df[(df["workload"] == wl) & (df["policy"] == pol) & df["ok"]][primary].astype(float)
            sets[pol] = {"runs": int(len(v)),
                         "relative_range": float((v.max() - v.min()) / v.mean()) if len(v) > 1 else None}
        ranges = [s["relative_range"] for s in sets.values() if s["relative_range"] is not None]
        out[scen] = {"workload": wl, "band": max(ranges) if ranges else None, "sets": sets}
    return out


def bar(matrix: pd.DataFrame, cfg: dict) -> dict:
    primary = cfg["primary"]
    static = [p for p, d in cfg["policies"].items() if d["kind"] == "static"]
    m = matrix[matrix["policy"].isin(static)]
    means = m.groupby("policy")[primary].mean().sort_values()
    per_wl = m.loc[m.groupby("workload")[primary].idxmin()].set_index("workload")["policy"]
    return {"S*": str(means.index[0]), "static_means": {k: float(v) for k, v in means.items()},
            "best_static_per_workload": per_wl.to_dict()}


def guards(v: pd.Series, s: pd.Series, g: dict) -> dict:
    out = {}
    for q in SIZES:
        a, b = float(v[f"wait_mean_min_{q}"]), float(s[f"wait_mean_min_{q}"])
        out[f"class_{q}"] = bool(a <= g["class_wait_ratio"] * b) if not math.isnan(a + b) else True
    out["utilization"] = bool(float(v["util"]) >= float(s["util"]) - g["utilization_drop"])
    out["jain"] = bool(float(v["jain_bsld"]) >= float(s["jain_bsld"]) - g["jain_drop"])
    return out


def compare(df: pd.DataFrame, cfg: dict) -> dict:
    primary = cfg["primary"]
    df = df.copy()
    df["ok"] = df["ok"].astype(bool)
    matrix = df[(df["role"] == "matrix") & df["ok"]]
    bands = noise_bands(df, cfg)
    b = bar(matrix, cfg)
    s_star = b["S*"]
    adaptive = [p for p, d in cfg["policies"].items() if d["kind"] == "adaptive"]
    pairs, verdict = [], {}
    for pol in adaptive:
        verdict[pol] = {}
        for scen in sorted({w.split("-")[0] for w in cfg["workloads"]}):
            band = bands.get(scen, {}).get("band")
            rows = []
            for wl in [w for w in cfg["workloads"] if w.startswith(scen + "-")]:
                v = matrix[(matrix["workload"] == wl) & (matrix["policy"] == pol)]
                s = matrix[(matrix["workload"] == wl) & (matrix["policy"] == s_star)]
                if v.empty or s.empty:
                    rows.append({"workload": wl, "missing": True})
                    continue
                v, s = v.iloc[0], s.iloc[0]
                d = float(v[primary]) - float(s[primary])
                rel = d / float(s[primary])
                gd = guards(v, s, cfg["guards"])
                rows.append({"workload": wl, "policy": pol, primary: float(v[primary]),
                             f"S*_{primary}": float(s[primary]), "difference": d,
                             "relative": rel,
                             "better_beyond_noise": bool(band is not None and rel < -band),
                             "within_noise_or_better": bool(band is not None and rel <= band),
                             "guards": gd, "guards_hold": all(gd.values())})
            pairs += rows
            complete = rows and not any(r.get("missing") for r in rows)
            verdict[pol][scen] = {
                "effective": bool(complete and all(r["better_beyond_noise"] and r["guards_hold"]
                                                   for r in rows)),
                "not_worse": bool(complete and all(r["within_noise_or_better"] and r["guards_hold"]
                                                   for r in rows)),
                "complete": bool(complete), "band": band}
    answer = "no"
    for pol, v in verdict.items():
        x, y, n = (v.get(s, {}) for s in ("X", "Y", "N"))
        if x.get("effective") and y.get("effective") and n.get("not_worse"):
            answer = "yes"
            break
        if x.get("effective") or y.get("effective"):
            answer = "partly"
    images = sorted(df["image_id"].dropna().unique()) if "image_id" in df else []
    return {"primary": primary, "noise_bands": bands, "bar": b, "verdict": verdict,
            "answer": answer, "pairs": pairs, "images": images,
            "runs": {"total": int(len(df)), "ok": int(df["ok"].sum()),
                     "failed": df.loc[~df["ok"], ["workload", "policy", "role"]].to_dict("records")}}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--metrics", type=Path, default=OUT / "metrics.csv")
    a = p.parse_args()
    cfg = yaml.safe_load(EVAL.read_text())
    res = compare(pd.read_csv(ROOT / a.metrics), cfg)
    (OUT / "comparison.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps({k: res[k] for k in ("bar", "verdict", "answer", "runs")}, indent=2))


if __name__ == "__main__":
    main()
