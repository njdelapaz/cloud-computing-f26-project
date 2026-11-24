# B1 workloads: summary

The pipeline that turns `dataset/` into simulator workloads, plus the episode finder. Gate: `scripts/check_b1.sh`. Its full output is in `check_b1.txt`, where every number below can be found or recomputed.

## What was built

| File | What it does |
|---|---|
| `workloads/convert/filter_trace.py` | SPEC §9.2 filters, runtime cap, cache at `workloads/traces/filtered.parquet` plus `.counts.json` (about 35 s, 7 GB RAM) |
| `workloads/convert/sample.py` | one window + one seed → `jobs.csv` + `meta.json` (SPEC §13.2), checked with `scripts/validate.py`; `--convert` also writes the simulator input |
| `workloads/convert/to_sim.py` | `jobs.csv` → `jobs.events`, `users.sim`, `sacctmgr.script`, `sim.env` (standard library only). Supersedes `scripts/p1_speed_probe.py`, which is kept only to reproduce the P1 speed numbers |
| `workloads/convert/common.py` | shared constants: cluster size, partitions, trace coverage, calibration/evaluation split |
| `workloads/episodes/find_episodes.py` | W1, W2, W4, W6 → `episodes.csv` (SPEC §13.2) and `episodes_review.txt` |
| `scripts/check_b1.sh`, `scripts/check_b1.py` | the gate |
| `tests/test_workloads.py` | 17 unit tests: filter rules, sampler, converter, episode rules. The ones that need pandas skip under plain `python3`, so `check_p2.sh` still runs |

Run from `ver_0.2/` (in a worktree, point at the main checkout's data and venv):
```
PYTHON=.venv/bin/python scripts/check_b1.sh --dataset dataset
.venv/bin/python -I workloads/convert/sample.py --start 2024-03-12 --seed 4 --convert
```

**Window parameters (not decided yet):** the measured window length (`--length`) and the warm-up (`--warmup`) both default to **1 day**. SPEC §11 leaves them to B3/P4. The warm-up comes *before* the measured window: `window_start = start − warmup`, `warmup_end = start`, `window_end = start + length`. With 1 day each, a measured window can start from 2024-01-02 to 2024-06-22. The sampler refuses windows outside the span where the trace holds every job, 2023-12-31T23:00Z to 2024-06-23T22:00Z. The files are split by job *end* time, so jobs submitted after 2024-06-23 that ran past June are missing.

## Filter counts (SPEC §9.2)

| Stage | This pipeline | SPEC §9.2 |
|---|---|---|
| Source jobs | 23,539,150 | 23.5M |
| Dropped: other partitions | 54,083 | about 54k |
| Dropped: never started | 289,460 | about 287k |
| Dropped: zero elapsed time | 328,388 | about 331k |
| Dropped: too big for one node | 10,372 = 0.045% of jobs, 1.11% of core-hours (cores 1,832, memory 9,192, multi-node 740, which overlap) | 0.05% of jobs, 1.1% of core-hours |
| **Kept** | **22,856,847**, from 649 users in 94 accounts | about 22.9M, 649 users, 94 accounts |
| Runtime capped at the time limit | 1.661% of kept jobs, median 16 s over, 99.85% of them `timeout` jobs | 1.66%, 16 s |

"Never started" here means `started = 0` or no start time. About 2.6k jobs have `started = 0` but do have a start time, and SPEC §9.2's profiling seems to have counted them under zero elapsed. The two buckets add up to the same 617,848 either way. These jobs also have no `allocmem`, so they can't be kept regardless. The longest kept time limit is 7 days, and the smallest memory request is 1 MB.

## Sampling accuracy

**The method differs from plain Bernoulli sampling.** Keeping each job with a fixed p = target ÷ (the window's full load) lands within ±5% of 0.85 only about half the time. A few long, wide jobs dominate a day's core-hours; one 32-core, 3-day job is a fifth of a day's target. So the sampler instead:
- gives every job a seeded random key u ~ U(0,1)
- takes the measured window's jobs in increasing u until the window's offered load reaches 0.85
- sets p to the key of the job that gets it there, which is what `meta.json`'s `sampling_rate` records
- skips any job that would push the load more than 2% over (rare; the `skipped` column of `samples.csv` counts them)
- keeps warm-up jobs when u ≤ p

The result is still a uniform random sample of jobs whose offered load lands in [0.85, 0.867]. Offered load = Σ cores × runtime of jobs submitted in the measured window ÷ (512 × window length).

Gate windows, written to disk, validated and converted (`samples.csv`):

| Window | Length | Seed | Split | p | Jobs | Offered load | Error |
|---|---|---|---|---|---|---|---|
| 2024-01-15 | 1d | 1 | calibration | 0.0388 | 5,223 | 0.8500 | +0.00% |
| 2024-01-30 | 1d | 2 | calibration | 0.0302 | 6,092 | 0.8558 | +0.68% |
| 2024-02-12 | 1d | 3 | calibration | 0.0334 | 10,947 | 0.8501 | +0.01% |
| 2024-02-26 | 1d | 1 | calibration | 0.0213 | 2,606 | 0.8516 | +0.19% |
| 2024-03-12 | 1d | 4 | evaluation | 0.0369 | 10,623 | 0.8500 | +0.00% |
| 2024-03-12 | 1d | 5 | evaluation | 0.0335 | 9,627 | 0.8506 | +0.08% |
| 2024-04-09 | 1d | 6 | evaluation | 0.0358 | 10,728 | 0.8622 | +1.43% |
| 2024-05-14 | 1d | 7 | evaluation | 0.0286 | 6,283 | 0.8501 | +0.01% |
| 2024-06-11 | 1d | 8 | evaluation | 0.0112 | 2,267 | 0.8639 | +1.64% |
| 2024-06-21 | 1d | 2 | evaluation | 0.0375 | 7,107 | 0.8510 | +0.11% |
| 2024-02-05 | 3d | 1 | calibration | 0.0307 | 21,849 | 0.8500 | +0.00% |
| 2024-04-22 | 3d | 3 | evaluation | 0.0335 | 12,273 | 0.8500 | +0.00% |

The jobs counts include the 1-day warm-up. All 12 windows are within ±5%, and every `jobs.csv` and `meta.json` passes the validators.

The in-memory sweep covered 40 random days × seeds 1–8, for both 1- and 3-day windows, 640 samples in all.
- **Accuracy:** every sample lands within ±5% (max error +1.79%). Plain Bernoulli on the same windows gets 52% (1-day) and 58% (3-day) within ±5%, with errors up to 52%.
- **Bias by job size:** jobs worth up to 5% of the window's target are kept at 0.99–1.04× the nominal rate p, and jobs worth 5–20% at 0.90×.
- **The largest jobs:** jobs worth over 20% of a day's target are too rare in the sweep to measure (9 expected). An exploratory run on 30 other days, with 60 expected, kept them at about 0.45×. Getting the load right costs a little on the very largest jobs.

**The warm-up's load isn't controlled.** p is set by the measured window, so the warm-up runs at whatever load its own day had at that p. In the gate windows that ranged from 0.18 to 1.67, so some measured windows start with an empty cluster and others with a backlog. B3/P4 may want to account for this when choosing window lengths.

## Converter and simulator check

- **Per job:** one event line, `-dt <submit − window_start> -e submit_batch_job | -J jobid_<job_id> -sim-walltime <runtime_s> --uid=<user> -t <minutes> -N 1 -n <cores> --ntasks-per-node=<cores> --mem=<MB> -A <account> -p normal -q normal pseudo.job`. Time limits are rounded up to whole minutes already in `jobs.csv`.
- **Accounts:** `sacctmgr.script` gives every account the same fair-share (`Fairshare=100`), with users under the accounts they ran jobs in.
- **Start time:** the simulator submits an event at `TimeStart + SecondsBeforeFirstJob + dt`, and `scripts/sim_run.sh` passes `-dtstart 30`. So the converter writes `sim.env` with `TIME_START = window_start − 30`.
- **One short simulation** (not part of the gate): 20 real jobs from 2024-01-14, edited to include a 32-core job, a 120,000 MB job and a job capped at its 1-minute limit, run with `ver02/slurm-sim:p1` on `config/project` in 53 s. All 20 completed on one node, with exactly the requested cores, memory, time limit and runtime.
- **11 s submit lag:** every submit landed exactly **11 s** after its trace time. That is slurmsim's start-up lag before slurmctld's clock starts; the converter can't remove it. B2 should take submit times from the simulator's own output.

## Episodes

Volumes are submitted core-hours (cores × capped runtime) in fixed 6-hour UTC bins (00/06/12/18) from 2024-01-01 to 2024-07-01. With these definitions the starting thresholds reproduce SPEC §9.3 exactly:
- 58 W1 flood windows from 22 users, split 21/37 (calibration/evaluation)
- 141 W2 bins that merge into 91 episodes

This confirms these were the original profiling's definitions.

| Workload | Rule | Episodes | Calibration (Jan–Feb) | Evaluation (Mar–Jun) |
|---|---|---|---|---|
| W1 user flood → `fairness` | one user > 50% of a bin's core-hours, bin total > 1.5× the median (57,536 core-hours); consecutive same-user bins merge | **55** (58 bins, 22 users) | 18 | 37 |
| W2 memory-heavy burst → `util` | jobs > 8,000 MB/core > 3× their median share (0.090) of a bin's core-hours, **and** at least their median volume (5,436 core-hours per bin; added in this review, see below); consecutive bins merge | **84** (130 bins) | 38 | 46 |
| W4 sequence | W1 and W2 episodes back to back: each next onset ≥ 1 h after the previous onset and ≤ 6 h after the previous end, both kinds present, whole chain within 1 day (the default window length); chains don't overlap | **19** | 8 | 11 |
| W6 daily peak → `none` | 12:00–17:00 UTC, every day | **182** | 60 | 122 |

- **Onsets** are refined to the hour. Each is the first hour in the episode's bins that meets the rule at hourly resolution, moved earlier while the hours just before it, up to 6 h back, meet it too. The end is found the same way. Every W1 and W2 episode had at least one such hour.
- **Durations:**
  - W1 episodes last a median 1 h (1–5 h), because floods are submission bursts.
  - W2 episodes last a median 4.5 h (1–45 h).
  - W4 chains span a median 11 h (2–18 h).
- **Format of `detail`:** every row starts with `split=calibration` or `split=evaluation`, by onset. The `episodes.csv` columns are fixed by SPEC §13.2, so the tag goes there. Episodes that reach past 2024-06-23T22:00Z also carry `coverage=partial`: 2 W1, 6 W2 and 2 W4. Next come each episode's own numbers: user, jobs, share, × median; for W6, that day's peak ratio and weekday.
- **Jan 1:** one W1, one W2 and one W4 episode start on 2024-01-01, too early for a 1-day warm-up inside the trace.
- **Daily profile:** the busiest 5 hours are 12:00–17:00 UTC, with 32.7% of submitted core-hours (1.57× the average rate), matching SPEC §9.2's 33% and 1.6×. Day by day the peak varies: its ratio has a median of 1.43, and 56 of 182 days have no peak (ratio below 1), mostly weekends. W6 runs should pick weekdays with a strong `peak_ratio`.
- **Files:** the full list is in `episodes.csv` and the review with hour-by-hour views is in `episodes_review.txt`, both copied here by the gate.

## Review

I read every W1 and W2 episode's summary line, plus hour-by-hour tables for the largest, the median and the smallest episode of each kind (in `episodes_review.txt`).

**W1 floods look real; no change.** Each is one user dominating a busy bin, usually in a single hour of mass submission. Examples:
- #43, 2024-06-01 14:00: 4,995 ten-core, 2-hour jobs from one user in one hour, 38× the median hourly volume and 89% of it.
- #46, 2024-06-09 19:00: 2,847 one-core, 46-hour jobs, 91% of that hour.

The floods range from 71 to 21,248 jobs, median 1,002. One caution for B3: 11 floods have under 300 jobs, made of a few very large jobs. At a sampling rate of about 3.3% they shrink to a handful of jobs, and with the sampler's slight bias against the very largest jobs, some seeds may barely contain the flood. Prefer floods with many jobs (`jobs=` in `detail`).

**W2: the starting rule passed near-idle bins, so I added a volume floor.** Under the starting rule, 11 of the 141 flagged bins had memory-heavy core-hours *below* their median per bin, and all 11 had a total of at most 0.25× the median (0.02–0.25×). Their share was high only because almost nothing else was submitted. The worst, 2024-02-17 06:00–12:00, held 0.02× the median volume: a few hundred 6-minute jobs on an idle cluster, nothing that could fill a node's memory or strand its cores. So W2 now also requires the bin's memory-heavy core-hours to be at least their median per bin. That drops exactly those 11 bins, listed in `episodes_review.txt`, and takes W2 from 91 to **84** episodes. The starting rule is still available as `find_episodes.py --w2-min-heavy 0`.
- **The kept bursts:** their memory-heavy core-hours run a median 4.8× the usual volume. Example: #119, 2024-05-04, three separate hours at 86–91% memory-heavy share, each at 5–7.5× the median hourly volume (2,645 jobs at a median 10 GB/core).
- **Borderline but kept:** a few remain at low total volume, such as #89, 2024-02-22 01:00–06:00: 0.15× the median total, but a normal volume of memory-heavy work, 78% of what was submitted.

## Open points for the main session (SPEC not edited)
1. **SPEC §9.3 W2 rule and count:** add the volume floor ("and their core-hours are at least their median per bin") and change "91 episodes" to 84. W1's "58 windows, 22 different users" and "split 21 to 37" still hold; these count bins, which merge into 55 episodes.
2. **SPEC §9.2 sampling wording:** p is now set per seed (random keys taken in order until the load reaches the target, plus the 2% skip rule) rather than one p per window. It is still "keep each job with probability p", and p still averages about 3.3% (1.1–3.9% in the gate windows).
3. **SPEC §9.2 counts:** with `started = 0` counted as never started, the never-started and zero-elapsed counts are 289k and 328k (their total is unchanged).
4. **SPEC §13.2:** the split and coverage tags live in `episodes.csv`'s `detail`, and the warm-up comes before the measured window. Worth one line each in the spec.
5. **B3/P4:** window and warm-up lengths, both defaulting to 1 day here; the uncontrolled warm-up load; the 11 s submit lag.
