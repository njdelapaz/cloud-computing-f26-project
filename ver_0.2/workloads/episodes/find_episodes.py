#!/usr/bin/env python3
"""Find trigger episodes in the filtered trace and write episodes.csv (SPEC §9.3, §13.2).

All volumes are submitted core-hours, cores x runtime (capped), of the filtered trace, binned by
submit time into fixed 6-hour UTC bins (00-06, 06-12, 12-18, 18-24) from 2024-01-01 to 2024-07-01.

  W1 user flood (signal fairness): a bin where one user submits more than FLOOD_SHARE (0.5) of the
     core-hours and the bin's total is above FLOOD_TOTAL (1.5) x the median bin total. Consecutive
     flagged bins of the same user form one episode.
  W2 memory-heavy burst (signal util): a bin where jobs above 8 GB (8,000 MB) per core make up more
     than HEAVY_MULT (3) x their median share of the bin's core-hours, and (added in the B1 review)
     their core-hours are at least HEAVY_MIN_VOLUME (1) x their median per bin. Without the second
     condition, near-idle bins pass on a handful of memory-heavy jobs. --w2-min-heavy 0 gives the
     SPEC §9.3 starting rule. Consecutive flagged bins form one episode.
  W4 sequence (signal of its first episode): W1 and W2 episodes back to back. Taken in onset order,
     each next episode starts at least 1 h after the previous one's onset and no more than SEQ_GAP
     (6 h) after the previous one ends, the chain holds both kinds, and it fits in SEQ_SPAN (1 day,
     the default window length). Chains don't overlap.
  W6 daily peak (signal none): 12:00-17:00 UTC on every day; detail gives that day's peak ratio,
     the 5-hour rate over the day's average rate.
  W3 and W5 need BASE simulation runs and are found in P6.

Onsets are refined to the hour (SPEC §9.3): within an episode's bins, the first hour that meets the
episode's own rule at hourly resolution (the user holds over half the hour's core-hours; the
heavy-job share is above 3x its median and the heavy jobs' core-hours are at least 1/6 of their
median per bin), moved earlier while the hours just before it, up to 6 h
back, meet it too. The end is found the same way from the last such hour. If no single hour meets
the rule, the bins' own edges are used.

Every episode's detail starts with split=calibration (January-February onset) or
split=evaluation (March-June), then coverage=partial if it reaches past the span where the trace
holds every job (common.COVERAGE_END, 2024-06-23T22:00Z), then the episode's own numbers.

Usage: python -I workloads/episodes/find_episodes.py [--cache FILE] [--out DIR]
Writes DIR/episodes.csv and DIR/episodes_review.txt (default DIR: workloads/traces/episodes/).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

VER02 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VER02))
sys.path.insert(0, str(VER02 / "scripts"))
from workloads.convert import common as T  # noqa: E402
import validate  # noqa: E402

BIN_S, HOUR = 6 * 3600, 3600
FLOOD_SHARE, FLOOD_TOTAL = 0.5, 1.5
HEAVY_MB_PER_CORE, HEAVY_MULT, HEAVY_MIN_VOLUME = 8000, 3.0, 1.0
SEQ_GAP, SEQ_SPAN, SEQ_MIN_STEP = 6 * HOUR, 86400, HOUR
PEAK_START_H, PEAK_END_H = 12, 17
LOOK_H = 6
EPISODE_HOURS = (T.EPISODE_END - T.EPISODE_START) // HOUR


def load(cache):
    df = pq.read_table(cache, columns=["submit", "user", "cores", "runtime", "mem_mb"],
                       filters=[("submit", ">=", T.EPISODE_START), ("submit", "<", T.EPISODE_END)]).to_pandas()
    return prepare(df)


def prepare(df):
    """Add the derived columns the finders use: core-hours, heavy flag, 6-h bin and hour."""
    df = df[(df.submit >= T.EPISODE_START) & (df.submit < T.EPISODE_END)].copy()
    df["user"] = df.user.astype(str)
    df["ch"] = df.cores.astype("float64") * df.runtime.astype("float64") / 3600
    df["heavy"] = df.mem_mb / df.cores > HEAVY_MB_PER_CORE
    df["bin"] = ((df.submit - T.EPISODE_START) // BIN_S).astype("int64")
    df["hour"] = ((df.submit - T.EPISODE_START) // HOUR).astype("int64")
    return df


class Tables:
    """Per-bin and per-hour totals the finders share."""

    def __init__(self, df):
        self.nb = (T.EPISODE_END - T.EPISODE_START) // BIN_S
        self.nh = (T.EPISODE_END - T.EPISODE_START) // HOUR
        self.bin_total = df.groupby("bin").ch.sum().reindex(range(self.nb), fill_value=0.0)
        self.hour_total = df.groupby("hour").ch.sum().reindex(range(self.nh), fill_value=0.0)
        self.bin_user = df.groupby(["bin", "user"]).ch.sum()
        self.hour_user = df.groupby(["hour", "user"]).ch.sum()
        self.bin_jobs_user = df.groupby(["bin", "user"]).size()
        self.bin_heavy = df[df.heavy].groupby("bin").ch.sum().reindex(range(self.nb), fill_value=0.0)
        self.hour_heavy = df[df.heavy].groupby("hour").ch.sum().reindex(range(self.nh), fill_value=0.0)
        self.median_total = float(self.bin_total.median())
        self.heavy_share = self.bin_heavy / self.bin_total.where(self.bin_total > 0)
        self.median_heavy_share = float(self.heavy_share.median())
        self.median_heavy = float(self.bin_heavy.median())


def t_of_hour(h):
    return T.EPISODE_START + int(h) * HOUR


def refine(first_bin, last_bin, ok):
    """Hour-level onset and end for an episode spanning bins first_bin..last_bin.

    ok(h) says whether hour h meets the episode's rule. Returns (onset hour, end hour exclusive,
    refined?)."""
    h0, h1 = first_bin * 6, (last_bin + 1) * 6
    hits = [h for h in range(h0, h1) if ok(h)]
    if not hits:
        return h0, h1, False
    on, off = hits[0], hits[-1] + 1
    while on > max(0, hits[0] - LOOK_H) and ok(on - 1):
        on -= 1
    while off < min(EPISODE_HOURS, hits[-1] + 1 + LOOK_H) and ok(off):
        off += 1
    return on, off, True


def runs(bins, key=None):
    """Group sorted bin numbers into runs of consecutive bins with the same key."""
    out = []
    for b in bins:
        k = key(b) if key else None
        if out and b == out[-1][-1][0] + 1 and out[-1][-1][1] == k:
            out[-1].append((b, k))
        else:
            out.append([(b, k)])
    return [([b for b, _ in r], r[0][1]) for r in out]


def find_w1(tb, share=FLOOD_SHARE, total=FLOOD_TOTAL):
    top = tb.bin_user.groupby(level=0).max()
    top_user = tb.bin_user.groupby(level=0).idxmax().map(lambda x: x[1])
    s = top / tb.bin_total.loc[top.index]
    flagged = s[(s > share) & (tb.bin_total.loc[s.index] > total * tb.median_total)].index
    eps = []
    for bins, user in runs(list(flagged), key=lambda b: top_user[b]):
        def ok(h, user=user):
            ht = tb.hour_total.iloc[h]
            return ht > 0 and tb.hour_user.get((h, user), 0.0) / ht > share
        on, off, refined = refine(bins[0], bins[-1], ok)
        uch = sum(tb.bin_user.get((b, user), 0.0) for b in bins)
        tch = float(tb.bin_total.loc[bins].sum())
        eps.append({"workload": "W1", "signal": "fairness", "onset": t_of_hour(on), "end": t_of_hour(off),
                    "bins": bins, "refined": refined, "user": user,
                    "jobs": int(sum(tb.bin_jobs_user.get((b, user), 0) for b in bins)),
                    "share": uch / tch, "total_x_median": tch / len(bins) / tb.median_total,
                    "detail": f"user={user} bins={len(bins)} jobs={int(sum(tb.bin_jobs_user.get((b, user), 0) for b in bins))} "
                              f"share={uch / tch:.2f} total_x_median={tch / len(bins) / tb.median_total:.2f}"})
    return eps, len(flagged)


def w2_bins(tb, mult=HEAVY_MULT, min_heavy=HEAVY_MIN_VOLUME):
    thr = mult * tb.median_heavy_share
    mask = (tb.heavy_share > thr) & (tb.bin_heavy >= min_heavy * tb.median_heavy)
    return list(mask[mask].index), thr


def find_w2(tb, mult=HEAVY_MULT, min_heavy=HEAVY_MIN_VOLUME):
    flagged, thr = w2_bins(tb, mult, min_heavy)
    eps = []
    for bins, _ in runs(flagged):
        def ok(h):
            ht = tb.hour_total.iloc[h]
            hh = tb.hour_heavy.iloc[h]
            return ht > 0 and hh / ht > thr and hh >= min_heavy * tb.median_heavy / 6
        on, off, refined = refine(bins[0], bins[-1], ok)
        hch, tch = float(tb.bin_heavy.loc[bins].sum()), float(tb.bin_total.loc[bins].sum())
        eps.append({"workload": "W2", "signal": "util", "onset": t_of_hour(on), "end": t_of_hour(off),
                    "bins": bins, "refined": refined, "share": hch / tch,
                    "total_x_median": tch / len(bins) / tb.median_total,
                    "detail": f"bins={len(bins)} mem_heavy_share={hch / tch:.2f} "
                              f"x_median_share={hch / tch / tb.median_heavy_share:.1f} "
                              f"heavy_x_median={hch / len(bins) / tb.median_heavy:.1f} "
                              f"total_x_median={tch / len(bins) / tb.median_total:.2f}"})
    return eps, len(flagged)


def find_w4(episodes, gap=SEQ_GAP, span=SEQ_SPAN, min_step=SEQ_MIN_STEP):
    """Non-overlapping chains of W1/W2 episodes back to back, holding both kinds."""
    eps = sorted(episodes, key=lambda e: (e["onset"], e["workload"]))
    out, i = [], 0
    while i < len(eps):
        chain = [eps[i]]
        j = i + 1
        while j < len(eps):
            prev, nxt = chain[-1], eps[j]
            if nxt["onset"] - prev["onset"] < min_step:
                j += 1          # starts together with the previous one: not a sequence step
                continue
            if nxt["onset"] > prev["end"] + gap or max(e["end"] for e in chain + [nxt]) - chain[0]["onset"] > span:
                break
            chain.append(nxt)
            j += 1
        kinds = {e["workload"] for e in chain}
        if len(kinds) >= 2:
            out.append({"workload": "W4", "signal": chain[0]["signal"], "onset": chain[0]["onset"],
                        "end": max(e["end"] for e in chain), "parts": chain,
                        "detail": "seq=" + ">".join(f"{e['workload']}#{e['episode_id']}" for e in chain)})
            i = eps.index(chain[-1]) + 1
        else:
            i += 1
    return out


def find_w6(tb):
    day_h = tb.hour_total.to_numpy().reshape(-1, 24)
    profile = day_h.sum(axis=0)
    share5 = np.array([profile[np.arange(h, h + 5) % 24].sum() for h in range(24)]) / profile.sum()
    best = int(np.argmax(share5))
    eps = []
    for d, hours in enumerate(day_h):
        if hours.sum() <= 0:
            continue
        ratio = hours[PEAK_START_H:PEAK_END_H].mean() / hours.mean()
        t = T.EPISODE_START + d * 86400
        wd = pd.Timestamp(t, unit="s").day_name()[:3]
        eps.append({"workload": "W6", "signal": "none", "onset": t + PEAK_START_H * HOUR,
                    "end": t + PEAK_END_H * HOUR, "ratio": ratio,
                    "detail": f"daily_peak=12:00-17:00 peak_ratio={ratio:.2f} day={wd}"})
    prof = {"busiest_5h_start": best, "busiest_5h_share": float(share5[best]),
            "peak_12_17_share": float(share5[PEAK_START_H]),
            "peak_12_17_ratio": float(share5[PEAK_START_H] / (5 / 24)), "hourly": profile}
    return eps, prof


def label(e):
    tags = [f"split={T.split_of(e['onset'])}"]
    if e["end"] > T.COVERAGE_END or e["onset"] < T.COVERAGE_START:
        tags.append("coverage=partial")
    return " ".join(tags + [e["detail"]])


def write_csv(path, episodes):
    rows = ["episode_id,workload,signal,onset,end,detail"]
    for e in episodes:
        rows.append(f"{e['episode_id']},{e['workload']},{e['signal']},{e['onset']},{e['end']},{label(e)}")
    path.write_text("\n".join(rows) + "\n")


def hourly_table(df, tb, e, pad=LOOK_H):
    """Hour-by-hour view of one episode for the review, from pad hours before to pad hours after."""
    h0 = max(0, (e["onset"] - T.EPISODE_START) // HOUR - pad)
    h1 = min(tb.nh, (e["end"] - T.EPISODE_START) // HOUR + pad)
    lines = [f"    {'hour (UTC)':<17} {'total ch':>9} {'x med/h':>7} " +
             ("{:>9} {:>6} {:>5}".format("user ch", "share", "jobs") if e["workload"] == "W1"
              else "{:>9} {:>6} {:>5}".format("heavy ch", "share", "jobs"))]
    med_h = tb.median_total / 6
    for h in range(h0, h1):
        t = t_of_hour(h)
        ht = tb.hour_total.iloc[h]
        sel = df[df.hour == h]
        if e["workload"] == "W1":
            part = sel[sel.user == e["user"]]
        else:
            part = sel[sel.heavy]
        mark = "*" if e["onset"] <= t < e["end"] else " "
        lines.append(f"  {mark} {T.iso(t)[:16]:<17} {ht:9.0f} {ht / med_h:7.2f} {part.ch.sum():9.0f} "
                     f"{(part.ch.sum() / ht if ht else 0):6.2f} {len(part):5d}")
    return lines


def review(df, tb, w1, w2, w4, w6, prof, n_w1_bins, n_w2_bins, min_heavy, picks=3):
    out = ["# Episode review (generated by workloads/episodes/find_episodes.py)", ""]
    out.append(f"median 6-h bin total: {tb.median_total:,.0f} core-hours; median share of >8 GB/core jobs: "
               f"{tb.median_heavy_share:.3f}; their median core-hours per bin: {tb.median_heavy:,.0f}")
    start_bins, _ = w2_bins(tb, min_heavy=0)
    dropped = sorted(set(start_bins) - set(w2_bins(tb, min_heavy=min_heavy)[0]))
    out.append(f"W2 starting rule (SPEC §9.3): {len(start_bins)} bins -> {len(runs(start_bins))} episodes. "
               f"The volume floor ({min_heavy:g} x median heavy core-hours) drops {len(dropped)} bins:")
    for b in dropped:
        out.append(f"    {T.iso(T.EPISODE_START + b * BIN_S)[:16]}  total {tb.bin_total[b] / tb.median_total:.2f}x median, "
                   f"heavy share {tb.heavy_share[b]:.2f}, heavy core-hours {tb.bin_heavy[b] / tb.median_heavy:.2f}x median")
    for name, eps, nbins in (("W1", w1, n_w1_bins), ("W2", w2, n_w2_bins)):
        cal = sum(T.split_of(e["onset"]) == "calibration" for e in eps)
        out.append(f"{name}: {nbins} flagged bins -> {len(eps)} episodes ({cal} calibration, {len(eps) - cal} evaluation); "
                   f"{sum(not e['refined'] for e in eps)} kept their bin edges (no single hour met the rule)")
    out.append(f"W1 users: {len({e['user'] for e in w1})}")
    out.append(f"W4: {len(w4)} sequences ({sum(T.split_of(e['onset']) == 'calibration' for e in w4)} calibration)")
    out.append(f"W6: {len(w6)} days; busiest 5 h start at {prof['busiest_5h_start']:02d}:00 UTC with "
               f"{prof['busiest_5h_share']:.1%} of submitted core-hours; 12:00-17:00 holds "
               f"{prof['peak_12_17_share']:.1%} ({prof['peak_12_17_ratio']:.2f}x the average rate)")
    out += ["", "## All W1 and W2 episodes", ""]
    for e in w1 + w2:
        dur = (e["end"] - e["onset"]) / HOUR
        out.append(f"#{e['episode_id']:<4} {e['workload']} {T.iso(e['onset'])[:16]} +{dur:4.0f}h  {label(e)}")
    out += ["", "## W4 sequences", ""]
    for e in w4:
        out.append(f"#{e['episode_id']:<4} {T.iso(e['onset'])[:16]}..{T.iso(e['end'])[:16]}  {label(e)}")

    # Hour-by-hour views: the largest, the median-size and the smallest episode of each kind
    out += ["", "## Hour-by-hour views (* = inside onset..end)", ""]
    for name, eps in (("W1", w1), ("W2", w2)):
        by = sorted(eps, key=lambda e: e["total_x_median"] * len(e["bins"]))
        chosen = [by[-1], by[len(by) // 2], by[0]][:picks] if by else []
        for e in chosen:
            out.append(f"{name} #{e['episode_id']} ({T.iso(e['onset'])[:16]}): {label(e)}")
            out += hourly_table(df, tb, e)
            if name == "W1":
                jobs = df[(df.bin.isin(e["bins"])) & (df.user == e["user"])]
                out.append(f"    user's jobs in its bins: {len(jobs)}, cores median {jobs.cores.median():.0f} "
                           f"max {jobs.cores.max()}, runtime median {jobs.runtime.median() / 3600:.1f} h, "
                           f"mem/core median {(jobs.mem_mb / jobs.cores).median():.0f} MB")
            else:
                jobs = df[(df.bin.isin(e["bins"])) & df.heavy]
                out.append(f"    heavy jobs in its bins: {len(jobs)} from {jobs.user.nunique()} users "
                           f"(top user {jobs.groupby('user').ch.sum().max() / jobs.ch.sum():.0%} of their core-hours), "
                           f"mem/core median {(jobs.mem_mb / jobs.cores).median():.0f} MB, "
                           f"runtime median {jobs.runtime.median() / 3600:.1f} h")
            out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(T.DEFAULT_CACHE), help="filtered trace from filter_trace.py")
    ap.add_argument("--out", default=str(T.TRACES / "episodes"))
    ap.add_argument("--w2-min-heavy", type=float, default=HEAVY_MIN_VOLUME,
                    help="W2 volume floor, x the median memory-heavy core-hours per bin (0 = SPEC starting rule)")
    a = ap.parse_args()

    df = load(a.cache)
    tb = Tables(df)
    w1, n1 = find_w1(tb)
    w2, n2 = find_w2(tb, min_heavy=a.w2_min_heavy)
    eid = 1
    for e in sorted(w1 + w2, key=lambda e: (e["workload"], e["onset"])):
        e["episode_id"] = eid
        eid += 1
    w1.sort(key=lambda e: e["episode_id"])
    w2.sort(key=lambda e: e["episode_id"])
    w4 = find_w4(w1 + w2)
    w6, prof = find_w6(tb)
    for e in w4 + w6:
        e["episode_id"] = eid
        eid += 1

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "episodes.csv", w1 + w2 + w4 + w6)
    errors = validate.validate_episodes((out / "episodes.csv").read_text())
    if errors:
        raise SystemExit("episodes.csv failed validation:\n  " + "\n  ".join(errors[:20]))
    (out / "episodes_review.txt").write_text(review(df, tb, w1, w2, w4, w6, prof, n1, n2, a.w2_min_heavy))
    print(f"W1 {len(w1)} episodes from {n1} bins ({len({e['user'] for e in w1})} users), "
          f"W2 {len(w2)} episodes from {n2} bins, W4 {len(w4)}, W6 {len(w6)} -> {out / 'episodes.csv'}")


if __name__ == "__main__":
    main()
