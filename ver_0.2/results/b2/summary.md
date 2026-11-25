# B2: metrics

The metrics of SPEC §10 and §13.4, computed from one run's output folder, plus the 95% confidence interval over seeds. The gate is `scripts/check_b2.sh`, and its output is in `check_b2.txt`.

## What was built
- `analysis/metrics.py` is standard-library only.
  - One command per run: `analysis/metrics.py RUN_DIR --meta META.json [--jobs JOBS.csv] [--label NAME] [--json OUT] [--csv OUT]`.
  - It prints one row as JSON. `--csv` appends the row to a table and writes the header the first time.
  - `analysis/metrics.py aggregate ROW.json ...` gives the mean ± 95% CI per metric over seed rows. B3's runner can call `run_metrics()` and `aggregate()` directly.
  - The t table covers 2 to 11 seeds: t = 2.776 for the 5 evaluation seeds and 4.303 for the 3 calibration seeds.
- **Inputs**, read from the run folder that `scripts/sim_run.sh` writes:
  - Completion records: `log/jobcomp.log`. Its times are parsed as UTC, which is how the image writes them. This host runs on EST.
  - Nodes and their sizes: `etc/slurm.conf`, falling back to the project cluster.
  - The optional logs: `log/decisions.csv`, `log/placements.csv`, `log/queue_dumps.csv` and `log/cost.csv`. When one is missing, its columns are empty: `null` in JSON, blank in CSV.
  - Runtimes for bounded slowdown come from `jobs.csv`. Without it, they come from End − Start.
  - The P1 probe's older `meta.json` shape, `{time_start, days}`, is read as a window with no warm-up.
- **Speed:** about 0.7 s for a 6,000-job day, including its 45 MB cost log. The node metrics are one sweep over submit, start and end events.
- **Hand-computed trace:** `tests/data/b2/` holds a run folder and a trace with 7 jobs on the 3-node smoke cluster.
  - The full derivation is in its `README.md`, and every value is in `hand.json`.
  - Every file passes `scripts/validate.py`'s checks for its format.
- **Tests:** `tests/test_metrics.py` has 22 tests, all standard-library, so they also run under `check_p2.sh`'s plain python3. They cover:
  - every column of the hand trace, plus the `examples/` logs;
  - absent logs;
  - window edges: warm-up jobs, a job submitted at `window_end`, and log rows outside the window;
  - the stranded-core median for an even count, and the strict "below";
  - Kendall distance against brute force;
  - the CI and the CSV and JSON output.

## Output columns
| Group | Columns |
|---|---|
| Run info | `run`, `window_s`, `cluster_nodes`, `cluster_cores`, `cluster_mem_mb`, `jobs_total`, `jobs_measured`, `jobs_missing` (measured jobs in `jobs.csv` with no completion record), `trace_mismatches` (submit, cores or memory differ from `jobs.csv`), `runtime_source`, `users_measured` |
| General | `wait_mean_s`, `wait_median_s`, `wait_p95_s`, `wait_max_s`, `bsld_mean`, `makespan_s`, `throughput_per_day` |
| Fairness | `fair_jain_bsld`, `fair_wait_spread`, `fair_wait_worst_user_s`, `fair_wait_best_user_s` |
| Balance and utilization | `bal_load_sd`, `bal_load_max`, `util_cores`, `util_mem`, `stranded_core_h` |
| Overhead | `switches`, `swap_us_{mean,p95,max}`, `placements_n`, `score_n`, `score_us_{mean,p95,max}`, `churn_{n,mean,max}` |
| Scheduler impact (SPEC §8 level 3) | `sched_n`, `sched_us_{mean,p95,max}`, `sched_us_{before,after}_switch`, and the same six for `bf` |

## Gate
`scripts/check_b2.sh --data <main checkout>` passes:
1. **Hand trace:** `analysis/metrics.py`, run as a command, matches all **51 of 51** columns of `hand.json`.
2. **`examples/` logs:** `decisions.csv` matches 4 of 4 values (4 switches; `swap_us` mean 799.5, p95 810.35, max 812). `placements.csv` matches 5 of 5 (7 placements, 3 scored; `score_us` mean 12.33, p95 13.8, max 14).
3. **Absent logs:** a run folder holding only `jobcomp.log` gives all 24 overhead columns empty, and the 16 job and node metrics unchanged.
4. **Unit tests:** 69 pass (validators 30, workloads 17, metrics 22).

## Sanity check on real virtual-clock runs (not gating)
The runs are B1's two standard samples and the P1 probe day. All are stock BASE on the 16-node cluster. `real_runs.csv` holds the full rows.

| Metric | `s0130_stock_1` (2024-01-30, seed 2) | `s0212_stock_1` (2024-02-12, seed 3) | `pr_stock_1` (probe, 2024-01-15) |
|---|---|---|---|
| Measured jobs / users | 3,716 / 61 | 6,654 / 54 | 3,028 / 54 (no warm-up) |
| Wait mean / median / p95 / max (s) | 6,525 / 2,606 / 20,193 / 129,166 | 50.6 / 1 / 498.7 / 4,987 | 846.9 / 2 / 2,136.3 / 56,509 |
| Mean bounded slowdown | 35.8 | 1.25 | 19.8 |
| Jain over per-user mean bounded slowdown | 0.096 | 0.125 | 0.043 |
| Wait spread (worst ÷ best user mean wait) | ∞ (122,639 s ÷ 0) | ∞ (4,987 s ÷ 0) | ∞ (14,841 s ÷ 0) |
| Node load SD / max (time-averaged) | 0.057 / 1.000 | 0.276 / 1.000 | 0.250 / 0.977 |
| Core / memory utilization | 0.748 / 0.935 | 0.635 / 0.666 | 0.519 / 0.625 |
| Stranded core-hours (24 h window) | 3,048 | 220 | 891 |
| Makespan (s) / throughput (jobs per day) | 582,329 / 551 | 325,344 / 1,767 | 359,695 / 727 |
| Main-scheduler passes: n, real µs mean / p95 / max | 6,955: 2,794 / 6,684 / 12,686 | 11,138: 248 / 759 / 10,457 | 5,784: 443 / 1,307 / 11,287 |
| Backfill cycles: n, real µs mean / p95 / max | 2,868: 78,749 / 267,373 / 588,325 | 446: 6,774 / 17,484 / 24,975 | 1,137: 19,355 / 50,625 / 135,141 |

- **Consistency:**
  - The completion records match `jobs.csv` exactly: 0 jobs missing and 0 mismatches in submit, cores or memory. With the virtual clock, every submit lands at its trace time.
  - End − Start equals `runtime_s` except for 2 of 6,092 jobs on 01-30, which are off by 1 s.
  - The probe's mean wait, 847 s, matches PROGRESS's Option B result. Its p95 here is 2,136.3 rather than 2,137, because the percentile rules differ (proposal 5).
- **Independent recomputation:** with numpy, `check_b2.py` rebuilds per-second node arrays and per-interval pending sets. Its waits, utilization, load SD and max, and stranded core-hours agree with `metrics.py` to 9 significant digits on all three runs.
- **01-30 is memory-bound:**
  - Memory utilization is 0.93 against 0.75 for cores, mean wait is 1.8 h, and 127 cores on average are stranded beside full memory.
  - B1 targets 0.85 offered *core* load. On memory-heavy days, memory is what binds. That gives utilization-aware placement (U) a lot to work with, but it also means days with the same nominal load can differ widely in contention.
- **02-12 is light** (mean wait 51 s), and its node loads are uneven (SD 0.28). Stock cons_tres packs nodes when the cluster isn't full.
- **Makespan and throughput are set by the longest measured job.** 01-30's 6.7-day makespan is one long job's end. They describe the trace more than the scheduler.

## SPEC gaps and proposals (proposals only; SPEC is unchanged)
These are implemented as described, so B3 can use the code now. Each one needs the user's decision before SPEC §13.4 or §13.3 records it.

1. **Wait spread is infinite on every real run.** 6 to 12 users per run (6 of 61, 11 of 54 and 12 of 54) have a mean wait of 0, so worst ÷ best divides by 0. Options:
   - **Recommended:** floor the denominator at τ = 10 s, the same floor bounded slowdown uses: worst ÷ max(best, 10 s). That gives 12,264, 499 and 1,484 on the three runs.
   - Report worst − best in seconds instead.
   - Until a decision, `fair_wait_spread` is ∞, and the worst and best user waits are reported beside it. 0 ÷ 0, when nobody waited, gives 1.
2. **Jain over per-user mean bounded slowdown is dominated by users with one or a few short jobs.**
   - On 02-12, one user's single job (bounded slowdown 45) takes the index from 0.61 to 0.13. On 01-30, the top four users have 1 to 37 jobs each, with mean slowdowns of 276 to 1,283.
   - The policy F may not move such an index much (G5). Options:
     - count only users with at least *k* measured jobs;
     - weight users by core-hours;
     - keep the definition and judge it in P4.
   - No change in the code yet.
3. **Queue-dump format for churn** (A2 writes it): `log/queue_dumps.csv`, header `sim_time,phase,rank,job_id,priority`, with one row per pending job per dump.
   - It is written only when a switch changes the ordering weights.
   - `sim_time` is the switch's decision-log row. `phase` is `before` or `after` the priority recompute. `rank` runs 1, 2, … in the order the scheduler would consider the jobs.
   - Churn for one switch is the share of pairs of jobs in both dumps whose order flipped. Switches with fewer than 2 pending jobs give no value.
   - The metric columns are the mean and max over switches, and the count of switches with a value.
   - Once adopted, this needs a validator and an `examples/` file. The fixture for now is `tests/data/b2/run/log/queue_dumps.csv`.
4. **Job ids in the placement log and the queue dumps:** use Slurm's JobId, as in `jobcomp.log`.
   - `examples/placements.csv` uses 101–107, which reads like trace job ids.
   - The current metrics filter these logs by sim time, so it doesn't matter yet. It will once anything joins them with the completion records.
5. **Percentiles:** linear interpolation between the two nearest ranks, numpy's default.
   - `scripts/vclock.py`'s quick statistics take the value at index ⌊0.95 n⌋ instead (2,137 vs 2,136.3 on the probe day).
6. **Which log rows count:** decision, placement, queue-dump and cost rows count when their sim time falls in [`warmup_end`, `window_end`), the same window the time-averaged metrics use.
7. **`score_us` statistics** cover only the node choices that were scored, those with `score` set. `score_n` counts them, and `placements_n` counts all node choices. Stock placement logs 0, which would otherwise dilute the mean.
8. **"Just before and after each switch"** (SPEC §8 level 3) means the last cycle of each kind before the switch's sim time and the first at or after it. Each is averaged over the switches in the window.
9. **Stranded cores:**
   - A job is pending from its submit until its start.
   - The median is over every pending job, warm-up jobs included. With an even count, it is the mean of the middle two.
   - "Below" is strict.
   - 128 GB means `RealMemory` = 128,000 MB.
10. **Wording, SPEC §13.4:** "throughput: finished measured jobs per day of makespan" counts every measured job that has a completion record. Measured jobs with no record are reported as `jobs_missing`; there were 0 in every run here.

Also noticed, outside B2's scope: `scripts/check_a1.py` and `scripts/vclock.py` parse jobcomp times with `fromisoformat(...).timestamp()`, which treats them as local time. That shifts every time by the same 5 h on this host, which is harmless for the differences those scripts compute but wrong as absolute times.
