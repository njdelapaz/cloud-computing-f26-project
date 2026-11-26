"""Tests for the priority weight configurations (task 1.6)."""
import itertools
from pathlib import Path

import pandas as pd
import pytest
import yaml

from sim import weights

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config/weights.yaml").read_text())
SCALES = yaml.safe_load((ROOT / "config/factor_scales.yaml").read_text())
FACTOR_KEYS = {"fairness": ["Fairshare"], "utilization": ["TRES_CPU", "TRES_Mem"],
               "load_balance": ["JobSize"]}
SCALE_KEYS = {"Fairshare": "fairshare", "TRES_CPU": "tres_cpu", "TRES_Mem": "tres_mem",
              "JobSize": "jobsize"}


@pytest.mark.parametrize("x, expected", [([0, 1], 1.0), ([1, 1, 1], 0.0), ([0, 0, 1], 2 / 3),
                                         ([3], 0.0)])
def test_gini_mean_difference(x, expected):
    assert weights.gini_mean_difference(x) == pytest.approx(expected)


def test_job_factors_match_slurm_formulas():
    f = weights.job_factors(pd.DataFrame({"cpus": [1, 32], "mem_mb": [4000, 128000],
                                          "timelimit_min": [60, 1]}))
    assert f["tres_cpu"].tolist() == pytest.approx([1 / 512, 32 / 512])
    assert f["tres_mem"].tolist() == pytest.approx([4000 / 2_048_000, 128000 / 2_048_000])
    # Single-node jobs count as one node's 32 CPUs, so only the time limit matters.
    assert f["jobsize"].tolist() == pytest.approx([32 / 60 / 512, 32 / 1 / 512])


def test_configs_are_all_orderings():
    orders = sorted(map(tuple, CFG["configs"].values()))
    assert orders == sorted(itertools.permutations(CFG["goals"]))


@pytest.mark.parametrize("name", list(CFG["configs"]))
def test_tier_swings_follow_order(name):
    w = weights.weights_for(CFG["configs"][name], CFG, SCALES)
    swing = {g: sum(w[k] * SCALES[SCALE_KEYS[k]] for k in FACTOR_KEYS[g]) for g in FACTOR_KEYS}
    for goal, tier in zip(CFG["configs"][name], CFG["tiers"]):
        assert swing[goal] == pytest.approx(tier, rel=0.01)
    assert w["Age"] == CFG["age_weight"]


@pytest.mark.parametrize("name", ["B0"] + list(CFG["configs"]))
def test_rendered_files_are_current(name):
    text = (ROOT / "config/priority" / f"{name}.conf").read_text()
    body = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert all(l.startswith("PriorityWeight") for l in body)
    assert len(body) == 4
    if name == "B0":
        assert all(l.split("=", 1)[1] in ("0", "CPU=0,Mem=0") for l in body)
    else:
        w = weights.weights_for(CFG["configs"][name], CFG, SCALES)
        assert body == weights.conf_text(name, "", w).splitlines()[2:]
