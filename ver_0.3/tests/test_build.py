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
    size = j["class"].astype(str).str.split("-").str[0]
    assert (j["qos"] == size.map({"sm": "small", "med": "medium", "lg": "large"})).all()
    assert all(l.endswith(f"-q {q} pseudo.job") for l, q in zip(lines, j["qos"]))


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


@needs_build
@pytest.mark.parametrize("d", scenario_dirs("Y"), ids=lambda d: d.name)
def test_overlay_bursts_match_intent(d):
    m = json.loads((d / "manifest.json").read_text())
    j = pd.read_parquet(d / "jobs.parquet")
    base = j[j["segment"] == "base"]
    assert set(base["submit_s"] // 86400) == set(range(7))          # background every day
    assert abs(m["achieved"]["rho"] / m["target_rho"] - 1) <= 0.10
    assert m["hours_with_2plus_bursts"] > 0                         # bursts do overlap
    for b in m["bursts"]:
        jobs = j[j["segment"] == f"burst{b['burst']:02d}:{b['name']}"]
        assert len(jobs) == b["jobs"] >= 1
        lo, hi = b["start_h"] * 3600, (b["start_h"] + b["hours"]) * 3600
        assert jobs["submit_s"].between(lo, hi - 1).all(), b
        classes = set(jobs["class"].astype(str))
        if b["name"] == "small-short flood":
            assert classes == {"sm-short"}
        elif b["name"] == "large-long heavy":
            assert classes == {"lg-long"}
        elif b["name"] == "memory-heavy medium":
            assert classes <= {"med-short", "med-med", "med-long"}
            assert (jobs["mem_mb"] * 32 > jobs["cpus"] * 128000).all()
        elif b["name"] == "one-account burst":
            assert set(jobs["account"]) == {m["burst_account"]}


# --------------------------------------------------------------------------- one-day D (Phase 4)

DAY = 86400


def day_pool(n=60000, seed=0):
    """Synthetic trace jobs over weeks 0-15 with a time of day, account and source id."""
    df = synthetic(n, seed)
    rng = np.random.default_rng(seed + 1)
    df["week"] = rng.integers(0, 16, n)
    df["tod"] = rng.integers(0, DAY, n)
    df["submit"] = build.trace.WEEK0 + df["week"] * WEEK + rng.integers(0, 7, n) * DAY + df["tod"]
    acc = rng.choice([f"a{i}" for i in range(8)], n, p=[.3] + [.1] * 7)
    df["account"] = pd.Categorical(acc)
    df["user"] = pd.Categorical(["u" + a for a in acc])
    df["src_jobid"] = np.arange(n)
    df["src_state"] = "COMPLETED"
    return df


POOL = day_pool()


def day_cfg(**over):
    import yaml
    cfg = yaml.safe_load(build.CONFIG.read_text())["day"]["D"]
    return {**cfg, "pool_weeks": [1, 2, 3], **over}


def make_day(pattern, seed=1, **over):
    return build.day_workload(POOL, "D", pattern, day_cfg(**over), seed, 0.95)


def test_day_windows_keep_time_of_day_and_pool_weeks():
    kept, info = make_day("nat")
    lead = 12 * 3600
    li, day = kept[kept["segment"] == "leadin"], kept[kept["segment"] == "nat"]
    assert len(li) and len(day) and len(li) + len(day) == len(kept)
    assert li["submit_s"].between(0, lead - 1).all()
    assert (li["submit_s"] == li["tod"] - (DAY - lead)).all()        # last 12 h of a day
    assert day["submit_s"].between(lead, lead + DAY - 1).all()
    assert (day["submit_s"] == lead + day["tod"]).all()             # midnight to midnight
    assert set(kept["week"]) <= {1, 2, 3}
    assert info["measured_window_s"] == [lead, lead + DAY]
    assert abs(max(info["day"]["rho_cpu"], info["day"]["rho_mem"]) / 0.95 - 1) <= 0.02
    assert abs(max(info["leadin"]["rho_cpu"], info["leadin"]["rho_mem"]) / 0.95 - 1) <= 0.02


@pytest.mark.parametrize("pattern,classes", [("flood", {"sm-short"}), ("lglong", {"lg-long"})])
def test_day_class_patterns(pattern, classes):
    kept, _ = make_day(pattern)
    day = kept[kept["segment"] == pattern]
    assert len(day) and set(day["class"].astype(str)) == classes


def test_day_memory_heavy_medium():
    kept, _ = make_day("mem")
    day = kept[kept["segment"] == "mem"]
    assert set(day["class"].astype(str)) <= {"med-short", "med-med", "med-long"}
    assert (day["mem_mb"] * 32 > day["cpus"] * 128000).all()


def test_day_one_account_burst():
    kept, info = make_day("acct")
    day = kept[kept["segment"] == "acct"]
    share = day.loc[day["account"] == info["burst_account"], "mem_s"].sum() / day["mem_s"].sum()
    assert info["burst_account"] == "a0" and 0.4 <= share <= 0.6


def test_day_alternating_blocks():
    kept, _ = make_day("alt")
    day = kept[kept["segment"] == "alt"]
    block = (day["submit_s"] - 12 * 3600) // (6 * 3600)
    assert set(block) == {0, 1, 2, 3}
    for b, cls in day.groupby(block)["class"]:
        assert set(cls.astype(str)) == ({"sm-short"} if b % 2 == 0 else {"lg-long"})


def test_day_overlapping_bursts_stay_in_the_day():
    kept, info = make_day("over")
    lead = 12 * 3600
    assert len(info["bursts"]) == 3
    base = kept[kept["segment"] == "base"]
    assert len(base) and base["submit_s"].between(lead, lead + DAY - 1).all()
    for b in info["bursts"]:
        jobs = kept[kept["segment"] == f"burst{b['burst']:02d}:{b['name']}"]
        assert len(jobs) == b["jobs"] >= 1 and 3 <= b["hours"] <= 12
        lo = lead + b["start_h"] * 3600
        assert b["start_h"] + b["hours"] <= 24
        assert jobs["submit_s"].between(lo, lo + b["hours"] * 3600 - 1).all(), b


def test_day_leadin_shared_by_patterns_and_deterministic():
    a, _ = make_day("nat", seed=1)
    b, _ = make_day("flood", seed=1)
    c, _ = make_day("nat", seed=2)
    a2, _ = make_day("nat", seed=1)
    lead = lambda k: sorted(k.loc[k["segment"] == "leadin", "src_jobid"])
    assert lead(a) == lead(b) and lead(a) != lead(c)
    assert a.equals(a2)


def test_day_without_leadin():
    kept, info = make_day("nat", leadin_hours=0)
    assert (kept["segment"] == "nat").all() and info["leadin"] is None
    assert kept["submit_s"].between(0, DAY - 1).all()


@needs_build
@pytest.mark.parametrize("d", scenario_dirs("D-"), ids=lambda d: d.name)
def test_day_workloads_match_intent(d):
    """Built D workloads (SPEC §9.4). Load bounds are loose: one large-long job can carry up
    to 0.41 of a day's memory load, and thinning keeps the prefix closest to target."""
    m = json.loads((d / "manifest.json").read_text())
    j = pd.read_parquet(d / "jobs.parquet")
    lo, hi = m["measured_window_s"]
    assert set(m["pool_weeks"]) <= set(range(1, 13))
    li, day = j[j["segment"] == "leadin"], j[j["segment"] != "leadin"]
    assert li["submit_s"].between(0, lo - 1).all() and day["submit_s"].between(lo, hi - 1).all()
    assert len(day) == m["day"]["jobs"] and len(li) == m["leadin"]["jobs"]
    target, pat = m["target_rho"], m["pattern"]
    rho = max(m["day"]["rho_cpu"], m["day"]["rho_mem"])
    assert 0.8 <= max(m["leadin"]["rho_cpu"], m["leadin"]["rho_mem"]) / target <= 1.1
    if pat not in ("alt", "over"):                 # blocks or bursts are at target, the day is not
        assert 0.8 <= rho / target <= 1.1, rho
    classes = set(day["class"].astype(str))
    if pat == "flood":
        assert classes == {"sm-short"}
    elif pat == "lglong":
        assert classes == {"lg-long"}
    elif pat == "mem":
        assert classes <= {"med-short", "med-med", "med-long"}
        assert (day["mem_mb"] * 32 > day["cpus"] * 128000).all()
    elif pat == "acct":
        mem = day["mem_mb"] * day["runtime_s"]
        assert 0.4 <= mem[day["account"] == m["burst_account"]].sum() / mem.sum() <= 0.6
    elif pat == "alt":
        block = (day["submit_s"] - lo) // (6 * 3600)
        for b, cls in day.groupby(block)["class"]:
            assert set(cls.astype(str)) == ({"sm-short"} if b % 2 == 0 else {"lg-long"})
    elif pat == "over":
        assert len(day[day["segment"] == "base"]) and len(m["bursts"]) == 3
        for b in m["bursts"]:
            jobs = day[day["segment"] == f"burst{b['burst']:02d}:{b['name']}"]
            t0 = lo + b["start_h"] * 3600
            assert len(jobs) == b["jobs"] >= 1
            assert jobs["submit_s"].between(t0, t0 + b["hours"] * 3600 - 1).all(), b
