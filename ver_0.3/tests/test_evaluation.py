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
