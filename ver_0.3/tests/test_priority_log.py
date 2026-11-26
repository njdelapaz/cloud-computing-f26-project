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
[{ts}] priority/multifactor: _get_priority_internal: Weighted QOS priority is 0.000000 * 0 = 0.00
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


def test_timeline_counts_pending_and_allocated():
    a = pd.DataFrame({"Submit": [0, 0, 100], "Start": [0, 200, 200], "End": [300, 400, 250],
                      "NCPUS": [32, 16, 8], "mem_mb": [128000, 64000, 1000]})
    tl = timeline(a, step=100)
    assert tl["pending"].tolist()[:4] == [1, 2, 0, 0]        # t = 0, 100, 200, 300
    assert tl["cpu_frac"].tolist()[:4] == pytest.approx([32 / 512, 32 / 512, 56 / 512, 16 / 512])
