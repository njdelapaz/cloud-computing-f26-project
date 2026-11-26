"""Tests for the priority configurations (SPEC §5): size QOS, fairness, time limit."""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from sim import weights
from sim.prepare_run import read_qos

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config/weights.yaml").read_text())
SCALES = yaml.safe_load((ROOT / "config/factor_scales.yaml").read_text())


@pytest.mark.parametrize("x, expected", [([0, 1], 1.0), ([1, 1, 1], 0.0), ([0, 0, 1], 2 / 3),
                                         ([3], 0.0)])
def test_gini_mean_difference(x, expected):
    assert weights.gini_mean_difference(x) == pytest.approx(expected)


def test_job_factors_match_slurm_formulas():
    f = weights.job_factors(pd.DataFrame({"cpus": [1, 32], "mem_mb": [4000, 128000],
                                          "timelimit_min": [60, 1]}))
    # Single-node jobs count as one node's 32 CPUs, so only the time limit matters.
    assert f["jobsize"].tolist() == pytest.approx([32 / 60 / 512, 32 / 1 / 512])


def test_scales_for_prefers_per_config():
    sc = {"jobsize": 1.0, "fairshare": 0.2, "per_config": {"C3": {"jobsize": 0.5}}}
    assert weights.scales_for("C3", sc)["jobsize"] == 0.5
    assert weights.scales_for("C3", sc)["fairshare"] == 0.2
    assert weights.scales_for("C1", sc)["jobsize"] == 1.0


def test_configs_are_the_requested_four():
    want = {"C0": {"small": 0, "medium": 0, "large": 0},
            "C1": {"small": 0, "medium": 50, "large": 100},
            "C2": {"small": 100, "medium": 50, "large": 0},
            "C3": {"small": 0, "medium": 100, "large": 0}}
    assert {k: v["qos_priority"] for k, v in CFG["configs"].items()} == want
    assert CFG["size_qos"] == {"sm": "small", "med": "medium", "lg": "large"}
    weights.validate(CFG)


@pytest.mark.parametrize("bad", [{"small": 0, "medium": 0},                    # missing QOS
                                 {"small": -1, "medium": 0, "large": 0},       # negative
                                 {"small": 0.5, "medium": 0, "large": 0},      # not integer
                                 {"small": 0, "medium": 100, "large": 0}])     # duplicate of C3
def test_validate_rejects_bad_configs(bad):
    with pytest.raises(ValueError):
        weights.validate({**CFG, "configs": {**CFG["configs"], "X": {"qos_priority": bad}}})


def test_strict_bound_covers_shortest_limit():
    w = {"Age": 500, "Fairshare": 6000, "JobSize": 1_000_000}
    # a 1-minute limit gives the largest job-size factor, 32 / 1 / 512
    assert weights.strict_size_bound(w) == 500 + 6000 + 62_500


def test_qos_factors_and_step():
    assert weights.qos_factors({"small": 0, "medium": 50, "large": 100}) == \
        {"small": 0.0, "medium": 0.5, "large": 1.0}
    assert weights.qos_factors({"small": 0, "medium": 0, "large": 0}) == \
        {"small": 0.0, "medium": 0.0, "large": 0.0}
    assert weights.size_step({"small": 0, "medium": 50, "large": 100}) == 0.5
    assert weights.size_step({"small": 0, "medium": 100, "large": 0}) == 1.0
    assert weights.size_step({"small": 0, "medium": 0, "large": 0}) == 0.0


@pytest.mark.parametrize("name", list(CFG["configs"]))
def test_tier_swings(name):
    c = CFG["configs"][name]
    sc = weights.scales_for(name, SCALES)
    w = weights.weights_for(c, CFG, sc)
    step = weights.size_step(c["qos_priority"])
    if not step:                                   # neutral C0 has no size term
        assert w["QOS"] == 0
    elif CFG["tiers"]["size"] == "strict":         # one step beats everything else combined
        assert w["QOS"] * step > weights.strict_size_bound(w)
    else:
        assert w["QOS"] * step == pytest.approx(CFG["tiers"]["size"], rel=0.01)
    assert w["Fairshare"] * sc["fairshare"] == pytest.approx(CFG["tiers"]["fairness"], rel=0.01)
    assert w["JobSize"] * sc["jobsize"] == pytest.approx(CFG["tiers"]["time"], rel=0.01)
    assert w["TRES_CPU"] == w["TRES_Mem"] == 0 and w["Age"] == CFG["age_weight"]


@pytest.mark.parametrize("name", ["B0"] + list(CFG["configs"]))
def test_rendered_files_are_current(name):
    text = (ROOT / "config/priority" / f"{name}.conf").read_text()
    body = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert all(l.startswith("PriorityWeight") for l in body) and len(body) == 5
    qos = read_qos(ROOT / "config/priority" / f"{name}.qos")
    if name == "B0":
        assert body == weights.conf_text("B0", "", weights.B0_WEIGHTS).splitlines()[2:]
        assert set(qos.values()) == {0}
    else:
        c = CFG["configs"][name]
        w = weights.weights_for(c, CFG, weights.scales_for(name, SCALES))
        assert body == weights.conf_text(name, "", w).splitlines()[2:]
        assert qos == c["qos_priority"]
        # Slurm stores weights and the total priority as 32-bit unsigned integers; every
        # factor is at most 1, so the sum of weights bounds the priority.
        assert sum(w.values()) < 2**32
