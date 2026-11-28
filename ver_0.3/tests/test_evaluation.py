"""Tests for the Step 6 metrics and pass rules (SPEC §8.5-8.6), on synthetic tables."""
import math

import pandas as pd
import pytest
import yaml

from evaluation.compare import compare
from evaluation.metrics import EVAL, bounded_slowdown, fairness, jain, segments, service

CFG = yaml.safe_load(EVAL.read_text())


def jobs(rows):
    j = pd.DataFrame(rows, columns=["wait_s", "run_s", "qos", "class", "account", "segment", "Submit"])
    j["day"] = ((j["Submit"] - j["Submit"].min()) // 86400).astype(int)
    return j


def test_jain_index():
    assert jain(pd.Series([2.0, 2.0, 2.0])) == pytest.approx(1.0)
    assert jain(pd.Series([1.0, 0.0, 0.0, 0.0])) == pytest.approx(0.25)


def test_bounded_slowdown_floors_short_runs():
    j = jobs([(600, 60, "small", "sm-short", "a", "", 0), (3600, 3600, "large", "lg-med", "b", "", 0)])
    assert list(bounded_slowdown(j, 600)) == pytest.approx([(600 + 60) / 600, 2.0])


def test_service_by_size_and_class():
    j = jobs([(60, 600, "small", "sm-short", "a", "", 0), (180, 600, "small", "sm-med", "a", "", 0),
              (600, 600, "large", "lg-long", "b", "", 0)])
    s = service(j, 600)
    assert s["jobs"] == 3 and s["wait_mean_min"] == pytest.approx(280 / 60)
    assert s["wait_mean_min_small"] == pytest.approx(2.0) and s["wait_mean_min_large"] == 10.0
    assert math.isnan(s["wait_mean_min_medium"]) and s["wait_mean_min_sm-med"] == 3.0


def test_fairness_skips_small_accounts():
    rows = [(0, 600, "small", "sm-short", "a", "", 0)] * 20 + \
           [(600, 600, "small", "sm-short", "b", "", 0)] * 20 + \
           [(99999, 600, "small", "sm-short", "c", "", 0)] * 3          # under 20 jobs: left out
    f = fairness(jobs(rows), 600, 20)
    assert f["jain_accounts"] == 2 and f["jain_bsld"] == pytest.approx(9 / 10)   # x = 1 and 2


def test_segments_days_and_bursts():
    j = jobs([(60, 1, "small", "sm-short", "a", "base", 0),
              (180, 1, "small", "sm-short", "a", "burst03:small-short flood", 86400 + 5)])
    s = segments(j)
    assert s["wait_mean_min_day0"] == 1.0 and s["wait_mean_min_day1"] == 3.0
    assert s["wait_mean_min_base"] == 1.0 and s["wait_mean_min_burst"] == 3.0


def metrics_table(adaptive_factor: float, large_factor: float = 1.0, noise: float = 0.02):
    """C0 is the best static; A1 = C0 * adaptive_factor on X and Y, equal on N."""
    rows = []
    static = {"B0": 3.0, "B1": 1.5, "C0": 1.0, "C1": 1.3, "C2": 1.2, "C3": 1.4}
    for wl in CFG["workloads"]:
        scen = wl.split("-")[0]
        for pol in CFG["policies"]:
            if pol in static:
                f = static[pol]
            else:
                f = 1.0 if scen == "N" else adaptive_factor
            base = 100.0 * f
            row = {"workload": wl, "scenario": scen, "seed": int(wl[-1]), "policy": pol,
                   "role": "matrix", "ok": True, "image_id": "img", "wait_mean_min": base,
                   "util": 0.75, "jain_bsld": 0.3}
            for q in ("small", "medium", "large"):
                row[f"wait_mean_min_{q}"] = base * (large_factor if (q == "large" and pol not in static) else 1)
            rows.append(row)
    for wl in CFG["repeats"]["workloads"]:                  # two more runs of each repeat policy
        for pol in CFG["repeats"]["policies"]:
            m = next(r for r in rows if r["workload"] == wl and r["policy"] == pol)
            for role, k in (("pilot", 1 + noise), ("repeat", 1 - noise)):
                rows.append({**m, "role": role, "wait_mean_min": m["wait_mean_min"] * k})
    return pd.DataFrame(rows)


def test_compare_yes_when_adaptive_beats_bar_beyond_noise():
    res = compare(metrics_table(0.8), CFG)
    assert res["bar"]["S*"] == "C0"
    assert res["noise_bands"]["X"]["band"] == pytest.approx(0.04, rel=0.05)
    assert res["verdict"]["A1"]["X"]["effective"] and res["verdict"]["A1"]["N"]["not_worse"]
    assert res["answer"] == "yes"


def test_compare_no_when_effect_inside_noise():
    res = compare(metrics_table(0.97, noise=0.05), CFG)          # 3% better, band ~10%
    assert not res["verdict"]["A1"]["X"]["effective"] and res["answer"] == "no"


def test_compare_guard_blocks_starving_large_jobs():
    res = compare(metrics_table(0.8, large_factor=2.0), CFG)      # large waits 1.6x S*'s
    pair = next(p for p in res["pairs"] if p["workload"] == "X-s1" and p["policy"] == "A1")
    assert pair["better_beyond_noise"] and not pair["guards"]["class_large"]
    assert res["answer"] == "no"


def test_compare_missing_run_is_not_effective():
    df = metrics_table(0.8)
    df = df[~((df["workload"] == "Y-s2") & (df["role"] == "matrix"))]
    res = compare(df, CFG)
    assert not res["verdict"]["A1"]["Y"]["complete"] and res["answer"] == "partly"


def test_exposure_splits_each_wait_over_the_states():
    import numpy as np
    from evaluation.favored import exposure
    log = [{"t_sim": 100, "state_before": "mix", "state": "small"},
           {"t_sim": 200, "state_before": "small", "state": "large"}]
    ex = exposure(log, np.array([50.0, 150.0, 250.0]), np.array([150.0, 250.0, 250.0]))
    assert ex.loc[0, "mix"] == pytest.approx(0.5) and ex.loc[0, "small"] == pytest.approx(0.5)
    assert ex.loc[1, "small"] == pytest.approx(0.5) and ex.loc[1, "large"] == pytest.approx(0.5)
    assert ex.loc[2, "large"] == 1.0 and ex.loc[2, "small"] == 0.0     # no wait: state at submit


# --------------------------------------------------------------------------- Phase 4 stage choice

SETUP = yaml.safe_load((EVAL.parent / "evaluation_setup.yaml").read_text())


def setup_table(factors: dict, noise: float = 0.05, large_factor: dict | None = None):
    """Matrix runs of every stage-1 policy on all 21 workloads: mean wait = 100 * factor."""
    rows = []
    for wl in SETUP["workloads"]:
        for pol in SETUP["stages"][1]["policies"]:
            f = factors.get(pol, 1.0)
            row = {"workload": wl, "policy": pol, "role": "matrix", "ok": True,
                   "wait_mean_min": 100.0 * f, "util": 0.8, "jain_bsld": 0.4,
                   "m_spells_medium": 3, "m_spells_large": 1, "m_active_share": 0.1}
            for q in ("small", "medium", "large"):
                lf = (large_factor or {}).get(pol, 1.0) if q == "large" else 1.0
                row[f"wait_mean_min_{q}"] = 100.0 * f * lf
            rows.append(row)
    for wl in SETUP["repeats"]["workloads"]:                 # two more runs, +-noise
        for pol in SETUP["repeats"]["policies"]:
            m = next(r for r in rows if r["workload"] == wl and r["policy"] == pol)
            for role, k in (("repeat", 1 + noise), ("repeat2", 1 - noise)):
                rows.append({**m, "role": role, "wait_mean_min": m["wait_mean_min"] * k})
    return pd.DataFrame(rows)


def test_select_picks_the_lowest_score():
    from evaluation.select import score_stage
    r = score_stage(setup_table({"A4-tight-60": 0.7, "A4-middle-60": 0.9, "A4-loose-60": 1.0},
                                noise=0.02), SETUP, 1)
    assert r["mean_band"] == pytest.approx(0.04, rel=0.01)
    assert r["choice"] == "A4-tight-60" and r["ties_with_lowest"] == ["A4-tight-60"]
    assert r["settings"]["A4-tight-60"]["score"] == pytest.approx(0.7)
    nat = r["settings"]["A4-tight-60"]["by_pattern"]["D-nat"]
    assert nat["seeds_beating_beyond_noise"] == 3 and not nat["flag_never_fires"]


def test_select_ties_go_to_fewer_guard_failures_then_the_looser_level():
    from evaluation.select import score_stage
    # all within a 10% band of each other: a tie; tight starves large jobs (guard fails)
    t = setup_table({"A4-tight-60": 0.90, "A4-middle-60": 0.92, "A4-loose-60": 0.95},
                    noise=0.05, large_factor={"A4-tight-60": 2.0})
    r = score_stage(t, SETUP, 1)
    assert set(r["ties_with_lowest"]) == {"A4-tight-60", "A4-middle-60", "A4-loose-60"}
    assert r["choice"] == "A4-loose-60"                 # middle and loose: 0 failures; looser
    assert r["settings"]["A4-tight-60"]["guard_failures"] == 12     # one per workload


def test_select_records_an_override_of_the_rule():
    import copy
    from evaluation.select import score_stage
    t = setup_table({"A4-tight-60": 0.90, "A4-middle-60": 0.92, "A4-loose-60": 0.95},
                    noise=0.05, large_factor={"A4-tight-60": 2.0})
    cfg = copy.deepcopy(SETUP)
    cfg["stages"][1].pop("chosen", None)
    r = score_stage(t, cfg, 1)
    assert r["chosen"] == r["choice"] == "A4-loose-60" and not r["overridden"]
    assert r["override_reason"] is None
    cfg["stages"][1].update(chosen="A4-tight-60", chosen_reason="requested")
    r = score_stage(t, cfg, 1)
    assert r["choice"] == "A4-loose-60" and r["chosen"] == "A4-tight-60" and r["overridden"]
    assert r["override_reason"] == "requested"
    cfg["stages"][1]["chosen"] = "A4-middle-30"
    with pytest.raises(ValueError):
        score_stage(t, cfg, 1)


def test_setup_stage1_choice_is_tight():
    assert SETUP["stages"][1]["chosen"] == "A4-tight-60"     # L*, task 4.5 (requested)


def test_select_needs_every_workload():
    from evaluation.select import score_stage
    t = setup_table({"A4-tight-60": 0.5})
    t = t[~((t["workload"] == "D-alt-s2") & (t["policy"] == "A4-tight-60"))]
    r = score_stage(t, SETUP, 1)
    assert not r["settings"]["A4-tight-60"]["complete"] and r["choice"] != "A4-tight-60"
