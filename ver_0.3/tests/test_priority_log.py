"""Tests for the priority debug log parser and the run timeline helper."""
import pandas as pd
import pytest

from sim.priority_log import parse
from sim.runstats import timeline

BLOCK = """[{ts}] priority/multifactor: _get_priority_internal: Weighted Age priority is 0.100000 * 500 = 50.00
[{ts}] priority/multifactor: _get_priority_internal: Weighted Assoc priority is 0.000000 * 0 = 0.00
[{ts}] priority/multifactor: _get_priority_internal: Weighted Fairshare priority is 0.500000 * 13333 = 6666.50
[{ts}] priority/multifactor: _get_priority_internal: Weighted JobSize priority is 0.000208 * 4808918 = 1000.25
[{ts}] priority/multifactor: _get_priority_internal: Weighted Partition priority is 0.000000 * 0 = 0.00
[{ts}] priority/multifactor: _get_priority_internal: Weighted QOS priority is 0.500000 * 8000 = 4000.00
[{ts}] priority/multifactor: _get_priority_internal: Site priority is 0
[{ts}] priority/multifactor: _get_priority_internal: Weighted TRES:CPU is 0.001953 * 2097487.00 = 4096.65
[{ts}] priority/multifactor: _get_priority_internal: Weighted TRES:Mem is 0.001953 * 626688.00 = 1223.99
[{ts}] priority/multifactor: _get_priority_internal: Job {job} priority: 0 + 50 + 0.00 + 6666.50 + 1000.25 + 0.00 + 0.00 + 5321 - 0 = 13037.39
"""


def test_parse_keeps_snapshot_minutes_and_reads_tres(tmp_path):
    log = tmp_path / "slurmctld.log"
    log.write_text(
        BLOCK.format(ts="2024-01-29T00:00:30.0", job=1001) +
        BLOCK.format(ts="2024-01-29T00:30:30.0", job=1001) +     # not a snapshot minute
        BLOCK.format(ts="2024-01-29T01:00:10.0", job=1001) +
        BLOCK.format(ts="2024-01-29T01:00:20.0", job=1002) +
        "[2024-01-29T01:00:25.0] unrelated line\n")
    df = parse(log, every_s=3600)
    assert len(df) == 3
    assert df["snapshot"].nunique() == 2
    r = df.iloc[0]
    assert r["Fairshare_w"] == 13333 and r["JobSize_v"] == pytest.approx(1000.25)
    assert r["TRES_cpu_w"] == pytest.approx(2097487) and r["TRES_mem_v"] == pytest.approx(1223.99)
    assert r["priority"] == pytest.approx(13037.39)
    assert r["QOS_f"] == 0.5 and r["QOS_w"] == 8000 and r["QOS_v"] == 4000


def test_class_dominance_counts_cross_class_pairs():
    from sim.check_configs import class_dominance
    snap = pd.DataFrame({"qos": ["large", "large", "small", "small", "medium"],
                         "priority": [10.0, 3.0, 5.0, 1.0, 4.0]})
    f = {"small": 0.0, "medium": 0.5, "large": 1.0}
    pairs, wins = class_dominance(snap, f)
    # large>small: (10>5,10>1,3>1) 3 of 4; large>medium: 10>4 1 of 2; medium>small: 4>1 1 of 2
    assert (pairs, wins) == (8, 5)


def test_cluster_balance_on_node_state():
    from sim.runstats import cluster_balance, node_timeline
    # Two full-node jobs on n01 and n02 for 1000 s; other 14 nodes idle.
    a = pd.DataFrame({"Submit": [0, 0], "Start": [0, 0], "End": [1000, 1000],
                      "NCPUS": [32, 32], "mem_mb": [128000, 128000], "NodeList": ["n01", "n02"]})
    nt = node_timeline(a, step=100)
    first = nt[nt["t"] == 0].set_index("node")["u"]
    assert first["n01"] == 1.0 and first["n02"] == 1.0 and first.drop(["n01", "n02"]).eq(0).all()
    b = cluster_balance(a, until_s=1000, step=100)
    expected_sigma = (2 / 16 * (1 - 2 / 16)) ** 0.5           # std of sixteen 0/1 values
    assert b["sigma_u"] == pytest.approx(expected_sigma)
    assert b["spread_u"] == pytest.approx(1.0)
    assert b["util"] == pytest.approx(2 / 16)


def test_timeline_counts_pending_and_allocated():
    a = pd.DataFrame({"Submit": [0, 0, 100], "Start": [0, 200, 200], "End": [300, 400, 250],
                      "NCPUS": [32, 16, 8], "mem_mb": [128000, 64000, 1000]})
    tl = timeline(a, step=100)
    assert tl["pending"].tolist()[:4] == [1, 2, 0, 0]        # t = 0, 100, 200, 300
    assert tl["cpu_frac"].tolist()[:4] == pytest.approx([32 / 512, 32 / 512, 56 / 512, 16 / 512])
