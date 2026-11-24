#!/usr/bin/env python3
"""B1 gate checks (run by scripts/check_b1.sh after the filter and the episode finder).

  1. filter:    the filtered trace's counts match SPEC §9.2
  2. sampling:  sampled windows from every month, several seeds, land within ±5% of the 0.85
                target; each one passes the validators and converts to simulator input. A wider
                in-memory sweep checks the same on many more windows.
  3. episodes:  episodes.csv validates; W1 and the W2 starting rule reproduce SPEC §9.3's counts;
                the daily peak is 12:00-17:00 UTC; the review and summary are recorded
Writes results/b1/samples.csv, copies episodes.csv and episodes_review.txt into results/b1/, and
prints one PASS/FAIL line per check. Exit code 1 if any check fails.

Usage: python -I scripts/check_b1.py [--cache FILE] [--episodes DIR]
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02))
sys.path.insert(0, str(VER02 / "scripts"))
sys.path.insert(0, str(VER02 / "workloads" / "episodes"))
from workloads.convert import common as T  # noqa: E402
from workloads.convert import sample as S  # noqa: E402
from workloads.convert import to_sim  # noqa: E402
import find_episodes as E  # noqa: E402
import validate  # noqa: E402

RESULTS = VER02 / "results" / "b1"
GATE_TRACES = T.TRACES / "b1_gate"
TARGET, TOL = 0.85, 0.05
SIZE_BUCKETS = [0, 0.001, 0.01, 0.05, 0.2, 10]
SIZE_LABELS = ["<0.1%", "0.1-1%", "1-5%", "5-20%", ">20%"]

# (measured window start, length, seed): every month, calibration and evaluation, several seeds
WINDOWS = [("2024-01-15", "1d", 1), ("2024-01-30", "1d", 2), ("2024-02-12", "1d", 3), ("2024-02-26", "1d", 1),
           ("2024-03-12", "1d", 4), ("2024-03-12", "1d", 5), ("2024-04-09", "1d", 6), ("2024-05-14", "1d", 7),
           ("2024-06-11", "1d", 8), ("2024-06-21", "1d", 2), ("2024-02-05", "3d", 1), ("2024-04-22", "3d", 3)]

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def near(got, want, rel):
    return abs(got - want) <= rel * want


def rounds_to(frac, pct, places):
    return round(100 * frac, places) == pct


# ---------------------------------------------------------------- 1. filter counts
def check_filter(cache):
    c = json.loads(Path(str(cache) + ".counts.json").read_text())
    print(f"source {c['source_jobs']:,} jobs; dropped: partition {c['dropped_partition']:,}, never started "
          f"{c['dropped_never_started']:,}, zero elapsed {c['dropped_zero_elapsed']:,}, too big {c['dropped_too_big']:,} "
          f"({100 * c['too_big_job_frac']:.3f}% of jobs, {100 * c['too_big_core_hour_frac']:.2f}% of core-hours; "
          f"cores {c['too_big_by_reason']['cores']:,}, mem {c['too_big_by_reason']['mem']:,}, "
          f"multi-node {c['too_big_by_reason']['multinode']:,}); kept {c['kept_jobs']:,}")
    check("filter: about 22.9M jobs kept", near(c["kept_jobs"], 22.9e6, 0.01), f"{c['kept_jobs']:,}")
    check("filter: about 54k dropped for other partitions", near(c["dropped_partition"], 54e3, 0.02),
          f"{c['dropped_partition']:,}")
    # Jobs with started=0 but a start time count here as never started; the SPEC's profiling put
    # them under zero elapsed. The two buckets' total is the same either way.
    check("filter: about 287k never started", near(c["dropped_never_started"], 287e3, 0.02),
          f"{c['dropped_never_started']:,}")
    check("filter: about 331k zero elapsed", near(c["dropped_zero_elapsed"], 331e3, 0.02),
          f"{c['dropped_zero_elapsed']:,}")
    check("filter: never started + zero elapsed about 618k",
          near(c["dropped_never_started"] + c["dropped_zero_elapsed"], 618e3, 0.002),
          f"{c['dropped_never_started'] + c['dropped_zero_elapsed']:,}")
    check("filter: too big for one node is 0.05% of jobs", rounds_to(c["too_big_job_frac"], 0.05, 2),
          f"{100 * c['too_big_job_frac']:.3f}%")
    check("filter: too big for one node is 1.1% of core-hours", rounds_to(c["too_big_core_hour_frac"], 1.1, 1),
          f"{100 * c['too_big_core_hour_frac']:.2f}%")
    check("filter: 1.66% of kept jobs capped at their time limit, median 16 s over",
          rounds_to(c["capped_frac"], 1.66, 2) and c["capped_median_over_s"] == 16,
          f"{100 * c['capped_frac']:.3f}%, median {c['capped_median_over_s']:.0f} s, "
          f"{100 * c['capped_timeout_frac']:.2f}% timeout jobs")
    check("filter: 649 users in 94 accounts", (c["users"], c["accounts"]) == (649, 94),
          f"{c['users']} users, {c['accounts']} accounts")
    check("filter: single-node jobs only, limits up to 7 days",
          c["max_timelimit_s"] <= T.MAX_TIME_LIMIT_S and c["min_mem_mb"] >= 1,
          f"max time limit {c['max_timelimit_s'] / 86400:g} d")
    return c


# ---------------------------------------------------------------- 2. sampling
def measured_load(trace_dir):
    """Offered load of the measured window, recomputed from jobs.csv and meta.json alone."""
    meta = json.loads((trace_dir / "meta.json").read_text())
    work = 0
    with open(trace_dir / "jobs.csv", newline="") as f:
        for r in csv.DictReader(f):
            if int(r["submit"]) >= meta["warmup_end"]:
                work += int(r["cores"]) * int(r["runtime_s"])
    return work / (T.CORES * (meta["window_end"] - meta["warmup_end"])), meta


def check_events(trace_dir, meta):
    """The events file has one line per job, in submit order, with the job's own values."""
    lines = (trace_dir / "jobs.events").read_text().splitlines()
    with open(trace_dir / "jobs.csv", newline="") as f:
        jobs = list(csv.DictReader(f))
    if len(lines) != len(jobs):
        return False
    for line, j in zip(lines, jobs):
        tok = line.split()
        get = lambda k: tok[tok.index(k) + 1]
        if (int(get("-dt")) != int(j["submit"]) - meta["window_start"]
                or int(get("-t")) * 60 != int(j["time_limit_s"])
                or int(get("-sim-walltime")) != int(j["runtime_s"])
                or get("-N") != "1" or get("-n") != j["cores"]
                or f"--mem={j['mem_mb']}" not in tok or get("-J") != f"jobid_{j['job_id']}"):
            return False
    users = (trace_dir / "users.sim").read_text().splitlines()
    shares = {line.split()[-1] for line in (trace_dir / "sacctmgr.script").read_text().splitlines()
              if line.startswith("add account")}
    return len(users) == meta["users"] + 1 and shares == {f"Fairshare={to_sim.ACCOUNT_FAIRSHARE}"}


def validate_cli(kind, path, *extra):
    r = subprocess.run([sys.executable, "-I", str(VER02 / "scripts" / "validate.py"), kind, str(path), *extra],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


def check_samples(cache):
    rows = []
    for start, length, seed in WINDOWS:
        out = GATE_TRACES / f"{start}_{length}_w1d_s{seed}"
        st = S.run(T.parse_time(start), T.parse_duration(length), 86400, seed, TARGET, cache, out)
        to_sim.convert(out)
        load, meta = measured_load(out)
        ok_meta, _ = validate_cli("meta", out / "meta.json")
        ok_jobs, msg = validate_cli("jobs", out / "jobs.csv", "--meta", str(out / "meta.json"))
        ok_ev = check_events(out, meta)
        rows.append({"window": start, "length": length, "seed": seed, "split": T.split_of(T.parse_time(start)),
                     "sampling_rate": round(meta["sampling_rate"], 5), "jobs": meta["jobs"],
                     "measured_jobs": st["measured_jobs"], "users": meta["users"], "accounts": meta["accounts"],
                     "unsampled_load": st["unsampled_load"], "offered_load": round(load, 4),
                     "error_pct": round(100 * (load / TARGET - 1), 2), "warmup_load": st["warmup_offered_load"],
                     "skipped": st["skipped"], "valid": ok_meta and ok_jobs, "events_ok": ok_ev})
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "samples.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{'window':<11} {'len':>3} {'seed':>4} {'split':<11} {'p':>7} {'jobs':>6} {'load':>7} {'err%':>6} "
          f"{'warm-up':>7} {'users':>5} {'accts':>5}")
    for r in rows:
        print(f"{r['window']:<11} {r['length']:>3} {r['seed']:>4} {r['split']:<11} {r['sampling_rate']:>7.4f} "
              f"{r['jobs']:>6} {r['offered_load']:>7.4f} {r['error_pct']:>6.2f} {r['warmup_load']:>7.3f} "
              f"{r['users']:>5} {r['accounts']:>5}")
    months = {r["window"][:7] for r in rows}
    check(f"sampling: {len(rows)} windows over {len(months)} months, within ±5% of {TARGET}",
          all(abs(r["offered_load"] / TARGET - 1) <= TOL for r in rows) and len(months) == 6,
          f"errors {min(r['error_pct'] for r in rows):+.2f}% to {max(r['error_pct'] for r in rows):+.2f}%")
    check("sampling: every jobs.csv and meta.json passes scripts/validate.py", all(r["valid"] for r in rows))
    check("converter: every events file matches its jobs.csv (one node, --mem in MB, whole minutes), "
          "equal account shares", all(r["events_ok"] for r in rows))


def sweep(cache, n_days=40, seeds=range(1, 9)):
    """Many more windows, in memory: the sampler vs plain Bernoulli at p = target / full load."""
    df = pq.read_table(cache, columns=["submit", "cores", "runtime"]).to_pandas()
    sub = df.submit.to_numpy()
    rng = np.random.default_rng(2024)
    lines = []
    for length_d in (1, 3):
        L = length_d * 86400
        first, last = T.COVERAGE_START + 86400 + 3600, T.COVERAGE_END - L     # room for a 1-day warm-up
        days = rng.integers(-(-first // 86400), last // 86400 + 1, n_days) * 86400
        ours, bern = [], []
        kept, expected = np.zeros(len(SIZE_BUCKETS) - 1), np.zeros(len(SIZE_BUCKETS) - 1)
        for t in days:
            lo, hi = np.searchsorted(sub, [t, t + L])
            w = df.iloc[lo:hi].reset_index(drop=True)
            w["i"] = np.arange(len(w))
            x = (w.cores * w.runtime).to_numpy("float64")
            full = x.sum() / (T.CORES * L)
            bucket = np.digitize(x / (TARGET * T.CORES * L), SIZE_BUCKETS) - 1
            for seed in seeds:
                s, _, _, _ = S.sample(w, t, t + L, seed, TARGET)
                ours.append(S.offered_load(s.cores, s.runtime, L) / TARGET - 1)
                np.add.at(kept, bucket[s.i.to_numpy()], 1)
                np.add.at(expected, bucket, TARGET / full)
                keep = np.random.default_rng(seed).random(len(x)) < TARGET / full
                bern.append(x[keep].sum() / (T.CORES * L) / TARGET - 1)
        ours, bern = np.abs(ours), np.abs(bern)
        lines.append((length_d, len(ours), float(np.mean(ours <= TOL)), float(ours.max()),
                      float(np.mean(bern <= TOL)), float(bern.max()), kept, expected))
    for length_d, n, ok, mx, bok, bmx, kept, expected in lines:
        print(f"sweep {length_d}-day windows: {n} samples; sampler {ok:.1%} within ±5% (max error {mx:.2%}); "
              f"plain Bernoulli {bok:.1%} within ±5% (max error {bmx:.1%})")
        rates = ", ".join(f"{SIZE_LABELS[k]}: {kept[k] / expected[k]:.2f} (expected {expected[k]:,.0f})"
                          for k in range(len(kept)) if expected[k] > 0)
        print(f"    jobs kept / expected at rate p, by job size as a share of the window's target core-hours: {rates}")
    check("sampling sweep: every sample within ±5%", all(ok == 1.0 for _, _, ok, *_ in lines),
          f"{sum(n for _, n, *_ in lines)} samples over random 1- and 3-day windows, seeds 1-8")


# ---------------------------------------------------------------- 3. episodes
def check_episodes(cache, ep_dir):
    text = (ep_dir / "episodes.csv").read_text()
    check("episodes: episodes.csv passes scripts/validate.py", validate_cli("episodes", ep_dir / "episodes.csv")[0])
    rows = list(csv.DictReader(text.splitlines()))
    by = {}
    for r in rows:
        split = r["detail"].split()[0].split("=")[1]
        by.setdefault(r["workload"], {}).setdefault(split, 0)
        by[r["workload"]][split] += 1
    for wl in sorted(by):
        print(f"{wl}: {sum(by[wl].values())} episodes ({by[wl].get('calibration', 0)} calibration, "
              f"{by[wl].get('evaluation', 0)} evaluation)")
    check("episodes: every episode tagged calibration or evaluation by its onset",
          all(r["detail"].split()[0] == f"split={T.split_of(int(r['onset']))}" for r in rows))

    tb = E.Tables(E.load(cache))
    w1, n1 = E.find_w1(tb)
    bins = [b for e in w1 for b in e["bins"]]
    cal = sum(T.EPISODE_START + b * E.BIN_S < T.EVAL_START for b in bins)
    users = len({e["user"] for e in w1})
    check("episodes: W1 reproduces SPEC §9.3 (58 flood windows, 22 users, split 21/37)",
          (n1, users, cal, n1 - cal) == (58, 22, 21, 37), f"{n1} windows, {users} users, {cal}/{n1 - cal}")
    start_bins, _ = E.w2_bins(tb, min_heavy=0)
    n_start = len(E.runs(start_bins))
    w2_now = by.get("W2", {})
    check("episodes: W2 starting rule reproduces SPEC §9.3 (91 episodes)", n_start == 91,
          f"{len(start_bins)} bins -> {n_start} episodes; with the B1 volume floor: "
          f"{sum(w2_now.values())} episodes")
    _, prof = E.find_w6(tb)
    check("episodes: busiest 5 h are 12:00-17:00 UTC, with about 33% of core-hours (about 1.6x the average rate)",
          prof["busiest_5h_start"] == 12 and round(100 * prof["peak_12_17_share"]) == 33
          and round(prof["peak_12_17_ratio"], 1) == 1.6,
          f"{prof['peak_12_17_share']:.1%}, {prof['peak_12_17_ratio']:.2f}x")
    check("episodes: W4 sequences found", by.get("W4") is not None, f"{sum(by.get('W4', {}).values())} sequences")

    RESULTS.mkdir(parents=True, exist_ok=True)
    shutil.copy(ep_dir / "episodes.csv", RESULTS / "episodes.csv")
    shutil.copy(ep_dir / "episodes_review.txt", RESULTS / "episodes_review.txt")
    summary = RESULTS / "summary.md"
    s = summary.read_text() if summary.exists() else ""
    check("episodes: counts and review recorded in results/b1/summary.md",
          "## Episodes" in s and "## Review" in s and str(sum(w2_now.values())) in s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(T.DEFAULT_CACHE))
    ap.add_argument("--episodes", default=str(T.TRACES / "episodes"))
    a = ap.parse_args()
    print("== 1. filtered trace (SPEC §9.2)")
    check_filter(a.cache)
    print("\n== 2. sampling and conversion")
    check_samples(a.cache)
    sweep(a.cache)
    print("\n== 3. episodes (SPEC §9.3)")
    check_episodes(a.cache, Path(a.episodes))
    print(f"\n{'all B1 checks passed' if not failures else f'{len(failures)} check(s) failed'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
