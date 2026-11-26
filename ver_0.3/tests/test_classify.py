"""Boundary tests for the nine-class classification (SPEC §4)."""
import numpy as np
import pandas as pd
import pytest

from workloads.classify import class_summary, classify, load_config, size_class, time_class


@pytest.mark.parametrize("cpus, mem, expected", [
    (1, 1, "sm"),
    (2, 8000, "sm"),          # exactly 1/16 in both dimensions
    (3, 100, "med"),          # CPU just over 1/16
    (1, 8001, "med"),         # memory just over 1/16
    (8, 32000, "med"),        # exactly 1/4
    (9, 1, "lg"),
    (1, 32001, "lg"),
    (32, 128000, "lg"),       # full node
    (1, 100000, "lg"),        # memory-bound: 1 core but most of a node's memory
    (24, 1000, "lg"),         # CPU-bound: many cores, little memory
])
def test_size_boundaries(cpus, mem, expected):
    assert size_class([cpus], [mem])[0] == expected


@pytest.mark.parametrize("limit, expected", [
    (60, "short"), (14400, "short"), (14401, "med"),
    (86400, "med"), (86401, "long"), (604800, "long"),
])
def test_time_boundaries(limit, expected):
    assert time_class([limit])[0] == expected


def test_out_of_range_rejected():
    with pytest.raises(ValueError):
        size_class([33], [1])
    with pytest.raises(ValueError):
        size_class([1], [128001])
    with pytest.raises(ValueError):
        time_class([604801])


def test_classify_names_and_order():
    c = classify([1, 4, 32], [1000, 1000, 1000], [3600, 50000, 200000])
    assert list(c) == ["sm-short", "med-med", "lg-long"]
    assert list(c.categories) == load_config()["classes"]


def test_summary_shares_sum_to_one():
    df = pd.DataFrame({"cpus": [1, 8, 32], "mem_mb": [100, 30000, 128000],
                       "runtime_s": [60, 3600, 7200]})
    df["class"] = classify(df["cpus"], df["mem_mb"], [600, 600, 600])
    s = class_summary(df)
    assert len(s) == 9
    for col in ["jobs_share", "core_h_share", "mem_gbh_share"]:
        assert np.isclose(s[col].sum(), 1.0)
    assert s.loc["sm-short", "jobs"] == 1 and s.loc["sm-long", "jobs"] == 0
