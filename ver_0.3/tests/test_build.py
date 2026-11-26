"""Tests for the workload builder (task 1.5).

Unit tests use synthetic jobs. The output tests check the built scenarios in
workloads/traces/ and are skipped when they have not been built
(.venv/bin/python -m workloads.build).
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from workloads import build
from workloads.classify import classify

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "workloads" / "traces"
WEEK = 7 * 86400


def synthetic(n=20000, seed=0):
    rng = np.random.default_rng(seed)
    cpus = rng.choice([1, 2, 4, 8, 16, 32], n, p=[.6, .15, .1, .08, .05, .02])
    mem = (cpus * rng.choice([1000, 4000, 8000], n)).clip(max=128000)
    limit = rng.choice([3600, 36000, 200000], n)
    runtime = (limit * rng.random(n)).astype(int) + 1
    df = pd.DataFrame({"cpus": cpus, "mem_mb": mem, "timelimit_s": limit, "runtime_s": runtime})
    df["class"] = classify(df["cpus"], df["mem_mb"], df["timelimit_s"])
    df["cpu_s"] = df["cpus"] * df["runtime_s"]
    df["mem_s"] = df["mem_mb"] * df["runtime_s"]
    return df


def load_of(df, window=WEEK):
    return build.rho(df["cpu_s"].sum(), df["mem_s"].sum(), window)


def test_thin_hits_target():
    df = synthetic()
    full = load_of(df)
    kept, p = build.thin(df, np.random.default_rng(1), full * 0.3, WEEK)
    assert abs(load_of(kept) / (full * 0.3) - 1) < 0.02
    assert 0.2 < p < 0.4                      # roughly the kept fraction


def test_thin_is_deterministic_per_seed():
    df = synthetic()
    a, _ = build.thin(df, np.random.default_rng(5), load_of(df) * 0.5, WEEK)
    b, _ = build.thin(df, np.random.default_rng(5), load_of(df) * 0.5, WEEK)
    c, _ = build.thin(df, np.random.default_rng(6), load_of(df) * 0.5, WEEK)
    assert a.index.equals(b.index)
    assert not a.index.equals(c.index)


def test_thin_quota_and_base_load():
    df = synthetic()
    full = load_of(df)
    kept, _ = build.thin(df, np.random.default_rng(2), full * 0.1, WEEK, quota=50)
    counts = kept["class"].value_counts()
    avail = df["class"].value_counts()
    for cls, n in avail.items():
        assert counts.get(cls, 0) >= min(50, n)
    # With half the target already present as base load, about half as much is kept.
    half = full * 0.2 * build.CAP_MEM * WEEK
    k2, _ = build.thin(df, np.random.default_rng(3), full * 0.4, WEEK, base_mem_s=half,
                       base_cpu_s=0)
    assert load_of(k2) < full * 0.4


def test_thin_empty_and_short_supply():
    df = synthetic(100)
    kept, _ = build.thin(df, np.random.default_rng(0), 100.0, WEEK)   # unreachable target
    assert len(kept) == len(df)
    kept, p = build.thin(df.iloc[:0], np.random.default_rng(0), 0.5, WEEK)
    assert len(kept) == 0 and p == 0.0


# --------------------------------------------------------------------------- built outputs

BUILT = sorted(TRACES.glob("*-s*/manifest.json")) if TRACES.exists() else []
needs_build = pytest.mark.skipif(not BUILT, reason="scenarios not built")


def scenario_dirs(prefix=""):
    return [m.parent for m in BUILT if m.parent.name.startswith(prefix)]


@needs_build
@pytest.mark.parametrize("d", scenario_dirs(), ids=lambda d: d.name)
def test_built_scenario_is_valid(d):
    m = json.loads((d / "manifest.json").read_text())
    for f, h in m["sha256"].items():
        assert hashlib.sha256((d / f).read_bytes()).hexdigest() == h, f
    j = pd.read_parquet(d / "jobs.parquet")
    assert len(j) == m["jobs"]
    assert (j["cpus"].between(1, 32)).all() and (j["mem_mb"].between(1, 128000)).all()
    assert (j["timelimit_min"] <= 10080).all()
    assert ((j["runtime_s"] >= 1) & (j["runtime_s"] <= j["timelimit_min"] * 60)).all()
    assert (j["job_id"] == np.arange(1001, 1001 + len(j))).all()
    assert j["submit_s"].is_monotonic_increasing
    assert j["submit_s"].between(0, WEEK - 1).all()
    pairs = {tuple(l.split("\t")) for l in (d / "accounts.txt").read_text().splitlines()
             if l and not l.startswith("#")}
    assert set(zip(j["account"], j["user"])) == pairs
    lines = (d / "events.txt").read_text().splitlines()
    assert len(lines) == len(j)
    assert lines[0].startswith(f"-dt {j['submit_s'].iloc[0]} -e submit_batch_job | -J jobid_1001 ")


@needs_build
@pytest.mark.parametrize("d", scenario_dirs("K") + scenario_dirs("N"), ids=lambda d: d.name)
def test_natural_weeks_hit_target(d):
    m = json.loads((d / "manifest.json").read_text())
    assert abs(m["achieved"]["rho"] / m["target_rho"] - 1) <= 0.02


@needs_build
@pytest.mark.parametrize("d", scenario_dirs("X"), ids=lambda d: d.name)
def test_shifting_segments_match_intent(d):
    m = json.loads((d / "manifest.json").read_text())
    j = pd.read_parquet(d / "jobs.parquet")
    j["seg"] = j["segment"].str.split(":").str[0].astype(int)
    j["day"] = j["submit_s"] // 86400
    assert (j["seg"] == j["day"]).all()
    target = m["target_rho"]
    for s in m["segments"]:
        day = j[j["seg"] == s["segment"]]
        seg_rho = max(s["rho_cpu"], s["rho_mem"])
        if s["name"] != "alternating bursts":   # blocks are at target, the day is not
            assert abs(seg_rho / target - 1) <= 0.10, s
        classes = set(day["class"].astype(str))
        if s["name"] == "small-short flood":
            assert classes == {"sm-short"}
        elif s["name"] == "large-long heavy":
            assert classes == {"lg-long"}
        elif s["name"] == "memory-heavy medium":
            assert classes <= {"med-short", "med-med", "med-long"}
            assert (day["mem_mb"] * 32 > day["cpus"] * 128000).all()
        elif s["name"] == "one-account burst":
            mem = day["mem_mb"] * day["runtime_s"]
            share = mem[day["account"] == m["burst_account"]].sum() / mem.sum()
            assert 0.4 <= share <= 0.6
        elif s["name"] == "alternating bursts":
            block = (day["submit_s"] % 86400) // (6 * 3600)
            for b, cls in day.groupby(block)["class"]:
                assert set(cls.astype(str)) == ({"sm-short"} if b % 2 == 0 else {"lg-long"})
