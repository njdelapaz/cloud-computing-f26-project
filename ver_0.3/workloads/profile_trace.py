"""Task 1.3: profile the trace, check data assumptions, and pick weeks K and N.

Usage:  .venv/bin/python -m workloads.profile_trace

Writes to results/profile/:
  assumptions.csv       one row per SPEC §3.1 assumption check
  weekly.csv            per-week load, accounts, class mix and eligibility for K/N
  classes_by_month.csv  per-month class shares (jobs, core-hours, memory-hours)
  weekly.png            weekly offered load and active accounts
  selection.json        chosen K and N weeks and why
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from workloads import trace
from workloads.classify import class_summary, classify, load_config

OUT = trace.ROOT / "results" / "profile"

COLUMNS = ["state", "account", "user", "partition", "submit", "start", "end", "timelimit",
           "elapsed", "allocmem", "maxrss", "alloccpus", "allocgpus", "started", "el9",
           "hostnames"]

# Node types on the source cluster (IN2P3 2024 dataset paper, Table 1): memory per node, MB.
SOURCE_NODE_MEM_MB = {"a": 192_000, "b": 386_000, "d": 1_289_000, "g": 1_546_000,
                      "m": 1_546_000, "p": 128_000}

# Cluster-wide maintenance days in the first half of 2024 (same paper): running jobs killed.
MAINTENANCE_DAYS = ["2024-03-12", "2024-06-25"]


def check_assumptions(df: pd.DataFrame) -> list[dict]:
    """SPEC §3.1 assumption checks on the full (unfiltered) trace."""
    rows = []
    ran = df[df["started"] == 1]

    mism = (ran["end"] - ran["start"] != ran["elapsed"]).mean()
    rows.append({"check": "elapsed == end - start (jobs that ran)",
                 "value": f"{1 - mism:.4%} match", "ok": mism < 0.001})

    multi = df["hostnames"].fillna("").str.contains(",", regex=False)
    parts = sorted(df.loc[multi, "partition"].unique())
    rows.append({"check": "multi-node jobs only in hpc", "value": f"partitions: {parts}",
                 "ok": parts == ["hpc"]})

    # The p nodes (hpc partition) have exactly 128 GB, so their largest single-node request
    # pins the unit: just under 128,000 means MB (KB or GB would be off by 1000x).
    single = ran[~multi.loc[ran.index]]
    p_max = single.loc[single["hostnames"].str.startswith("p"), "allocmem"].max()
    rows.append({"check": "allocmem in MB (largest request on 128 GB p nodes)",
                 "value": f"{p_max:,.0f} MB", "ok": 64_000 < p_max <= 128_000})
    prefix = single["hostnames"].str[0]
    per_type = {p: single.loc[prefix == p, "allocmem"].max() for p in SOURCE_NODE_MEM_MB}
    rows.append({"check": "largest single-node allocmem per source node type (MB)",
                 "value": ", ".join(f"{p}={v:,.0f}" for p, v in per_type.items() if pd.notna(v)),
                 "ok": True})

    r = ran.loc[(ran["el9"] == 0) & ran["maxrss"].notna() & (ran["allocmem"] > 0)]
    ratio = (r["maxrss"] / r["allocmem"]).quantile(0.95)
    rows.append({"check": "maxrss in MB (p95 maxrss/allocmem, non-EL9 jobs)",
                 "value": f"{ratio:.3f}", "ok": 0.05 < ratio < 2.0})

    order = ((ran["submit"] <= ran["start"]) & (ran["start"] <= ran["end"])).mean()
    rows.append({"check": "submit <= start <= end", "value": f"{order:.4%} of jobs that ran",
                 "ok": order > 0.999})

    # Epoch seconds are absolute; the monthly files are cut at local (Europe/Paris) midnight.
    lo, hi = pd.to_datetime([df["end"].min(), df["end"].max()], unit="s", utc=True)
    lo_l, hi_l = lo.tz_convert("Europe/Paris"), hi.tz_convert("Europe/Paris")
    rows.append({"check": "end times span 2024-01 to 2024-06 in Europe/Paris time",
                 "value": f"{lo_l:%Y-%m-%d %H:%M} .. {hi_l:%Y-%m-%d %H:%M} local "
                          f"({lo:%Y-%m-%d %H:%M} .. {hi:%Y-%m-%d %H:%M} UTC)",
                 "ok": (lo_l.year, lo_l.month, hi_l.month) == (2024, 1, 6)})

    minutes = (df["timelimit"] % 60 == 0).mean()
    rows.append({"check": "timelimit in seconds (share that are whole minutes)",
                 "value": f"{minutes:.4%}", "ok": minutes > 0.99})
    return rows


def weekly_profile(jobs: pd.DataFrame, data_end: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-week offered load and class mix, for weeks starting 2024-01-01 (UTC)."""
    cfg = load_config()
    cap_cpu = trace.CLUSTER_CORES * trace.WEEK_S
    cap_mem = trace.CLUSTER_MEM_MB * trace.WEEK_S
    jobs = jobs[jobs["week"] >= 0].assign(
        cpu_s=lambda x: x["cpus"].astype("int64") * x["runtime_s"],
        mem_s=lambda x: x["mem_mb"] * x["runtime_s"])

    g = jobs.groupby("week")
    week = pd.DataFrame({
        "jobs": g.size(),
        "rho_cpu": g["cpu_s"].sum() / cap_cpu,
        "rho_mem": g["mem_s"].sum() / cap_mem,
        "accounts": g["account"].nunique(),
        "users": g["user"].nunique(),
    })
    week["rho"] = week[["rho_cpu", "rho_mem"]].max(axis=1)
    starts = trace.WEEK0 + week.index * trace.WEEK_S
    week.insert(0, "week_start", pd.to_datetime(starts, unit="s", utc=True).strftime("%Y-%m-%d"))

    # A week is complete if every job it submits must have ended (limit <= 7 d) before the
    # data ends; the monthly files hold jobs by end time, so later weeks lose their long jobs.
    week["complete"] = starts + 2 * trace.WEEK_S <= data_end
    maint = [(pd.Timestamp(d, tz="UTC").timestamp() - trace.WEEK0) // trace.WEEK_S
             for d in MAINTENANCE_DAYS]
    week["maintenance"] = week.index.isin(maint)
    week["holiday"] = week.index == 0
    week["eligible"] = week["complete"] & ~week["maintenance"] & ~week["holiday"]

    share_jobs = pd.crosstab(jobs["week"], jobs["class"], normalize="index").reindex(
        columns=cfg["classes"], fill_value=0)
    memh = jobs.assign(memh=jobs["mem_mb"] * jobs["runtime_s"])
    share_mem = memh.pivot_table(index="week", columns="class", values="memh",
                                 aggfunc="sum", observed=False).reindex(columns=cfg["classes"])
    share_mem = share_mem.fillna(0).div(share_mem.sum(axis=1), axis=0)
    mix = pd.concat({"jobs": share_jobs, "memh": share_mem}, axis=1)
    return week, mix


def typicality(week: pd.DataFrame, mix: pd.DataFrame) -> pd.Series:
    """Distance from the median eligible week; lower is more typical (SPEC §3.5)."""
    e = week[week["eligible"]]
    score = pd.Series(0.0, index=e.index)
    for col in ["rho", "rho_cpu", "accounts"]:
        q1, med, q3 = e[col].quantile([0.25, 0.5, 0.75])
        score += (e[col] - med).abs() / max(q3 - q1, 1e-9)
    for part in ["jobs", "memh"]:
        m = mix[part].loc[e.index]
        l1 = (m - m.median()).abs().sum(axis=1)
        score += l1 / max(l1.median(), 1e-9)
    return score.sort_values()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config()

    full, kept, by_month = [], [], []
    for m in trace.months():
        df = trace.load(COLUMNS, [m])
        full.append(df)
        f = df[trace.filter_mask(df)]
        j = pd.DataFrame({
            "month": m,
            "account": f["account"].astype("category"),
            "user": f["user"].astype("category"),
            "submit": f["submit"],
            "cpus": f["alloccpus"].astype("int32"),
            "mem_mb": f["allocmem"].astype("int64"),
            "timelimit_s": f["timelimit"],
            "runtime_s": trace.runtime_s(f),
        })
        j["class"] = classify(j["cpus"], j["mem_mb"], j["timelimit_s"], cfg)
        j["week"] = trace.week_of(j["submit"])
        kept.append(j)
        s = class_summary(j, cfg)
        s.insert(0, "month", m)
        by_month.append(s.reset_index())
        print(f"month {m}: {len(df):,} jobs, kept {len(f):,} ({len(f) / len(df):.1%})")

    df_all = pd.concat(full, ignore_index=True)
    checks = check_assumptions(df_all)
    data_end = int(df_all["end"].max())
    del full, df_all
    pd.DataFrame(checks).to_csv(OUT / "assumptions.csv", index=False)

    jobs = pd.concat(kept, ignore_index=True)
    for c in ["account", "user"]:
        jobs[c] = jobs[c].astype(str).astype("category")
    pd.concat(by_month, ignore_index=True).to_csv(OUT / "classes_by_month.csv", index=False,
                                                  float_format="%.5f")

    week, mix = weekly_profile(jobs, data_end)
    score = typicality(week, mix)
    week["typicality"] = score.reindex(week.index)
    out = week.join(mix["jobs"].add_prefix("jobshare_")).join(mix["memh"].add_prefix("memshare_"))
    out.to_csv(OUT / "weekly.csv", float_format="%.4f")

    n_week = int(score.index[0])
    k_week = int(next(w for w in score.index[1:] if abs(w - n_week) >= 2))
    selection = {
        "rule": "eligible = complete, no maintenance, not the Jan 1 week; N = most typical, "
                "K = next most typical at least 2 weeks away from N",
        "N": {"week": n_week, "start": week.loc[n_week, "week_start"],
              "typicality": round(float(score[n_week]), 3)},
        "K": {"week": k_week, "start": week.loc[k_week, "week_start"],
              "typicality": round(float(score[k_week]), 3)},
        "eligible_weeks": int(week["eligible"].sum()),
    }
    (OUT / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    plot(week, selection)

    print(pd.DataFrame(checks).to_string(index=False))
    print(week.drop(columns=["users"]).to_string(float_format=lambda v: f"{v:.2f}"))
    print(json.dumps(selection, indent=2))


def plot(week: pd.DataFrame, selection: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    x = week.index
    a1.plot(x, week["rho_mem"], marker="o", label="memory")
    a1.plot(x, week["rho_cpu"], marker="o", label="CPU")
    a1.set_ylabel("offered load vs\n16-node cluster (×)")
    a1.legend(frameon=False)
    a2.bar(x, week["accounts"], color="0.6")
    a2.set_ylabel("active accounts")
    a2.set_xlabel("week (from Mon 2024-01-01, UTC)")
    for ax in (a1, a2):
        for w in week.index[~week["eligible"]]:
            ax.axvspan(w - 0.5, w + 0.5, color="0.9", zorder=0)
        for key in ("K", "N"):
            ax.axvline(selection[key]["week"], color="k", ls="--", lw=0.8)
    a1.set_title("Trace weeks before thinning (grey: not eligible; dashed: K and N)")
    for key in ("K", "N"):
        a1.annotate(key, (selection[key]["week"], a1.get_ylim()[1]), ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(OUT / "weekly.png", dpi=120)


if __name__ == "__main__":
    main()
