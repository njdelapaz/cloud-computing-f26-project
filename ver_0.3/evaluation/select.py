"""Tasks 4.5-4.6: score a stage's A4 settings against B1 and choose one (SPEC §9.6).

Usage: .venv/bin/python -m evaluation.select --stage 1 [--config config/evaluation_setup.yaml]

From the config's metrics_out (results/phase4/metrics.csv), matrix runs that passed:
  noise band b_p   per pattern p, the larger relative range (max - min) / mean of mean wait
                   over the three runs of each repeat policy on the pattern's repeat workload
                   (seed 1: the matrix run and the repeat runs).
  ratio            per workload, a setting's mean wait / the reference's (B1).
  score            the geometric mean of a setting's ratios over the stage's workloads, so
                   every pattern counts the same whatever its wait scale.
  guards           per workload, against the reference: no size class's mean wait more than
                   25% higher, utilization at most 1 point lower, Jain at most 0.05 lower.
  choice           the lowest score. Settings whose score is within b (the mean of the
                   per-pattern bands) of the lowest, relative to it, tie. Among them: the
                   fewest guard failures, then the stage's tie_break (stage 1: the looser
                   target level; stage 2: the longer stabilization period).
  chosen           the setting carried forward: the stage's `chosen` in the config when the
                   user overrode the rule (recorded with `overridden: true`), else the choice.
Also, per setting and pattern: on how many seeds it beats the reference beyond the noise
band, the spells per day by state and the share of the measured days with a class favored.
A setting is flagged on a pattern if it never fires there (no spell on any seed), or keeps a
class favored for more than flag_active_share of the measured days on average.
Writes results/phase4/selection.json (key "stage<N>", other stages kept).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from evaluation.compare import guards

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "evaluation_setup.yaml"


def pattern_of(workload: str) -> str:
    return workload.rsplit("-s", 1)[0]


def noise_bands(df: pd.DataFrame, cfg: dict) -> dict:
    primary = cfg["primary"]
    out = {}
    for wl in cfg["repeats"]["workloads"]:
        sets = {}
        for pol in cfg["repeats"]["policies"]:
            v = df[(df["workload"] == wl) & (df["policy"] == pol) & df["ok"]][primary].astype(float)
            sets[pol] = {"runs": int(len(v)), "values": [round(x, 3) for x in v],
                         "relative_range": float((v.max() - v.min()) / v.mean()) if len(v) > 1 else None}
        ranges = [s["relative_range"] for s in sets.values() if s["relative_range"] is not None]
        out[pattern_of(wl)] = {"workload": wl, "band": max(ranges) if ranges else None, "sets": sets}
    return out


def score_stage(df: pd.DataFrame, cfg: dict, stage: int) -> dict:
    st = cfg["stages"][stage]
    primary, ref, g = cfg["primary"], cfg["reference"], cfg["guards"]
    df = df.copy()
    df["ok"] = df["ok"].astype(bool)
    matrix = df[(df["role"] == "matrix") & df["ok"]].set_index(["workload", "policy"])
    bands = noise_bands(df, cfg)
    band_vals = [b["band"] for b in bands.values() if b["band"] is not None]
    mean_band = float(np.mean(band_vals)) if band_vals else None
    settings = {}
    for pol in st["adaptive"]:
        per_wl, logs, fails = [], [], 0
        for wl in cfg["workloads"]:
            if (wl, pol) not in matrix.index or (wl, ref) not in matrix.index:
                per_wl.append({"workload": wl, "complete": False})
                continue
            v, s = matrix.loc[(wl, pol)], matrix.loc[(wl, ref)]
            ratio = float(v[primary]) / float(s[primary])
            gd = guards(v, s, g)
            band = bands.get(pattern_of(wl), {}).get("band")
            fails += sum(not ok for ok in gd.values())
            logs.append(math.log(ratio))
            per_wl.append({"workload": wl, "complete": True, "ratio": ratio,
                           "beats_beyond_noise": bool(band is not None and ratio < 1 - band),
                           "worse_beyond_noise": bool(band is not None and ratio > 1 + band),
                           "guards": gd,
                           "spells_medium": v.get("m_spells_medium"),
                           "spells_large": v.get("m_spells_large"),
                           "active_share": v.get("m_active_share")})
        complete = all(r["complete"] for r in per_wl)
        by_pat = {}
        for pat in dict.fromkeys(pattern_of(w) for w in cfg["workloads"]):
            rows = [r for r in per_wl if r["complete"] and pattern_of(r["workload"]) == pat]
            if not rows:
                continue
            spells = [(r["spells_medium"] or 0) + (r["spells_large"] or 0) for r in rows]
            act = [r["active_share"] or 0 for r in rows]
            by_pat[pat] = {
                "ratio_geomean": float(np.exp(np.mean([math.log(r["ratio"]) for r in rows]))),
                "seeds_beating_beyond_noise": sum(r["beats_beyond_noise"] for r in rows),
                "seeds_worse_beyond_noise": sum(r["worse_beyond_noise"] for r in rows),
                "spells_per_day_medium": float(np.mean([r["spells_medium"] or 0 for r in rows]) / 2),
                "spells_per_day_large": float(np.mean([r["spells_large"] or 0 for r in rows]) / 2),
                "active_share_mean": float(np.mean(act)),
                "guard_failures": sum(not ok for r in rows for ok in r["guards"].values()),
                "flag_never_fires": bool(sum(spells) == 0),
                "flag_mostly_active": bool(np.mean(act) > cfg.get("flag_active_share", 0.5))}
        settings[pol] = {"complete": complete, "workloads": len(logs),
                         "score": float(np.exp(np.mean(logs))) if logs else None,
                         "guard_failures": fails, "by_pattern": by_pat, "per_workload": per_wl}
    ranked = sorted((p for p in settings if settings[p]["complete"]),
                    key=lambda p: settings[p]["score"])
    choice, ties = None, []
    if ranked:
        best = settings[ranked[0]]["score"]
        ties = [p for p in ranked if mean_band is not None
                and (settings[p]["score"] - best) / best <= mean_band] or [ranked[0]]
        order = st.get("levels", {})
        # fewest guard failures, then the looser level / the longer period (higher order)
        choice = sorted(ties, key=lambda p: (settings[p]["guard_failures"], -order.get(p, 0)))[0]
    chosen = st.get("chosen") or choice
    if chosen is not None and chosen not in settings:
        raise ValueError(f"stage {stage}: chosen {chosen!r} is not one of its adaptive settings")
    return {"stage": stage, "reference": ref, "primary": primary,
            "noise_bands": bands, "mean_band": mean_band,
            "ranking": [{"policy": p, "score": settings[p]["score"],
                         "guard_failures": settings[p]["guard_failures"]} for p in ranked],
            "ties_with_lowest": ties, "choice": choice,
            "chosen": chosen, "overridden": bool(chosen != choice),
            "override_reason": st.get("chosen_reason") if chosen != choice else None,
            "all_complete": all(s["complete"] for s in settings.values()),
            "settings": settings}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--stage", type=int, required=True)
    p.add_argument("--config", type=Path, default=CFG)
    a = p.parse_args()
    cfg = yaml.safe_load((ROOT / a.config).read_text())
    df = pd.read_csv(ROOT / cfg["metrics_out"])
    res = score_stage(df, cfg, a.stage)
    out = ROOT / Path(cfg["metrics_out"]).parent / "selection.json"
    allres = json.loads(out.read_text()) if out.exists() else {}
    allres[f"stage{a.stage}"] = res
    out.write_text(json.dumps(allres, indent=1, default=float) + "\n")
    print(json.dumps({k: res[k] for k in ("mean_band", "ranking", "ties_with_lowest", "choice",
                                          "chosen", "overridden", "all_complete")},
                     indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
