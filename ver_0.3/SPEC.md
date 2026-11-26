# SPEC — Adaptive Slurm Priority Weights (ver_0.3)

This document specifies Steps 1–3 of the `Project` brief in this folder: workloads, job
classification and priority weight configurations. Steps 4–6 (trigger states, the change
mechanism, and simulation) will be specified after Steps 1–3 are complete.
[PLAN.md](PLAN.md) lays out the order of work.

**Scope rule (from the brief):** everything lives in and refers only to `ver_0.3/`. All paths
in this document are relative to `ver_0.3/`.

---

## 1. Goal and research question

Build a mechanism that switches Slurm's multifactor priority weights while the cluster runs, and
simulate it to answer:

> Can switching priority weights based on the live cluster state adapt to changing workloads
> and provide better overall service than any single fixed weight setting?

What counts as "better" will be defined with the Step 6 specification.

### Specified in this document
- Turning the trace into workloads that fit the simulated cluster (Step 1).
- A 9-class job classification (Step 2).
- Six priority weight configurations, one per ordering of three goals (Step 3).

### Specified later
- The cluster states that trigger a configuration change (Step 4).
- The configuration change mechanism (Step 5).
- Simulation of the full setup and its evaluation (Step 6).

### Out of scope
- Changing node selection or placement (`SelectType`, `LLN`, node weights). Only priority
  settings change.
- GPUs, multiple partitions, multiple QOS, job dependencies, reservations, preemption.
- Changing what jobs request. Jobs keep their trace CPU, memory and time-limit requests.
- Running on a real cluster. Everything is tested in the UB Slurm Simulator.

---

## 2. System under study

### 2.1 Simulated cluster

| Item | Value |
|---|---|
| Nodes | 16, `n01`–`n16` |
| Per node | 2 sockets × 16 cores = 32 cores, `RealMemory=128000` (MB), no GPUs |
| Totals | 512 cores, 2,048,000 MB (2 TB) |
| Memory per core | 4,000 MB |
| Partition | `normal`, all nodes, `MaxTime=7-00:00:00`, default |
| QOS | one, `normal` |
| Scheduler | Slurm default (`SchedulerType=sched/backfill`, default backfill parameters) |
| Resource selection | `select/cons_tres` with `CR_Core_Memory`, so memory is a consumable resource |
| Priority plugin | `priority/multifactor` |
| Priority recalculation | `PriorityCalcPeriod=1` (every minute; tunable later) |
| Accounting | `slurmdbd`, needed for fair-share usage |
| Weekly capacity | 86,016 core-hours and 344,064 GB-hours |

### 2.2 Static `slurm.conf` priority keys

These stay fixed for a run. Only the weight keys in §5 change.

```
PriorityType=priority/multifactor
PriorityCalcPeriod=1
PriorityDecayHalfLife=7-0          # Slurm default; tunable later
PriorityMaxAge=1-0                 # age factor saturates after one day
PriorityFavorSmall=YES             # job-size factor favors small jobs (§5.1)
PriorityFlags=SMALL_RELATIVE_TO_TIME
PriorityWeightPartition=0          # one partition, constant
PriorityWeightQOS=0                # one QOS, constant
Include priority_weights.conf      # holds the PriorityWeight* lines of one configuration (§5)
```

### 2.3 Accounts and fair share
- One Slurm account for each trace `account`, and one user association for each
  (user, account) pair seen in the workload.
- Equal shares: every account has `RawShares=1` under `root`, and every user has `RawShares=1`
  within their account. The default fair-share algorithm (Fair Tree) is used.
- Fairness is considered at the **account** level first and the user level second.

---

## 3. Input data and workload builder (Step 1)

### 3.1 Trace

`dataset/01.tsv` … `06.tsv`: one file per month, from 2024-01-01 to 2024-06-30. There are
23,539,150 jobs in about 3.1 GB of tab-separated text with a header row. The files are the
first half of the public IN2P3 Computing Center 2024 workload dataset (Cochard & Simon,
arXiv:2606.05914), which documents the fields. Each monthly file holds the jobs that *ended*
that month, cut at local (Europe/Paris) midnight. Timestamps are epoch seconds.

| Column | Meaning | Use |
|---|---|---|
| `jobid` | trace job id | provenance |
| `state` | completed, failed, cancelled, timeout, out_of_memory, node_fail | reporting only |
| `account`, `user` | anonymized ids | fair-share entities |
| `partition` | htc, hpc, htc_highmem, flash, gpu, gpu_interactive, htc_interactive, htc_daemon | filter |
| `submit`, `eligible`, `start`, `end` | epoch seconds | submit time; start/end for validation |
| `timelimit` | requested limit, seconds | `-t` |
| `elapsed` | actual run time, seconds | simulated run time |
| `allocmem` | allocated memory, MB (assumed; confirmed in Phase 1) | `--mem` |
| `alloccpus` | allocated cores | `-n` |
| `allocgpus` | GPUs | filter |
| `started` | 1 if the job ever ran | filter |
| `hostnames` | node list | filter (multi-node) |
| `maxrss`, `totalcpu`, `maxdiskwrite`, `nsteps`, `el9` | — | unused |

Profile of January (from a pass over `01.tsv`), for orientation:
- 3,504,369 jobs, 399 users, 78 accounts, 291 distinct hosts on the source cluster.
- 95% of jobs use 1 core, and 99.3% are in `htc`.
- The first week (Jan 1–7) offers about **17×** this cluster's weekly CPU capacity and about
  **28×** its memory capacity, counting actual run time.
- Jobs average about 6.6 GB per core-hour, against 4 GB per core on these nodes. **Memory is
  the bottleneck:** when memory is fully allocated, CPU allocation tops out near 60%. This
  shapes the utilization goal (§5).

Confirmed in Phase 1 (`results/profile/assumptions.csv`):
- `allocmem` and `maxrss` are in MB. The largest request on the 128 GB `hpc` nodes is
  125,000.
- `elapsed = end − start` for 99.997% of jobs that ran.
- Only `hpc` jobs span several nodes.
- `timelimit` is in seconds and always a whole number of minutes.
- Over all six months, a week offers 21–37× this cluster's memory capacity and 14–36× its CPU
  capacity (`results/profile/weekly.csv`).

The simulator's job-trace format, `--mem`, `-t` and per-job `--uid` are confirmed by its
documentation; the smoke run checks them in practice.

### 3.2 Filters
A trace job enters a workload only if all of these hold:

1. `partition` ∈ {htc, hpc, htc_highmem, flash}. GPU, interactive and daemon partitions are
   dropped.
2. `allocgpus = 0` and `started = 1`. Jobs that never ran are dropped.
3. It fits on one node: `hostnames` names one host, `alloccpus ≤ 32`, `allocmem ≤ 128000`.
4. `timelimit ≤ 604800` (7 days, the partition `MaxTime`).

In January these filters keep 98.2% of jobs.

### 3.3 Field mapping

| Workload field | Rule |
|---|---|
| `submit_s` | `submit − window_start` (seconds from the start of the scenario) |
| `runtime_s` | `max(1, min(elapsed, timelimit))` |
| `timelimit_min` | `ceil(timelimit / 60)`; Slurm limits are in minutes |
| `cpus` | `alloccpus` |
| `mem_mb` | `allocmem` (per job, single node) |
| `user`, `account` | unchanged trace ids |
| `class` | from §4 |
| `src_jobid`, `src_state` | provenance |

Every job's partition and QOS become `normal`.

### 3.4 Scaling to fit the cluster
The aggregate load must also fit, not just each job. Offered load over the submission window
`T`:

```
ρ_cpu = Σ cpus·runtime  / (512 · T)
ρ_mem = Σ mem_mb·runtime / (2,048,000 · T)
ρ     = max(ρ_cpu, ρ_mem)        # in practice ρ_mem
```

- **Method:** seeded Bernoulli thinning. Each filtered job is kept with probability `p`, and `p`
  is solved so that `ρ` equals the target `ρ*`. Thinning keeps the trace's daily and weekly
  shape and each account's relative share.
- **Default target:** `ρ* = 0.95`. Queues form at peaks and drain in troughs, which gives
  priority something to decide. The builder accepts any `ρ*`, so other load levels can be
  built later.
- **Optional class quotas:** thin per class with separate rates, to guarantee a minimum count
  per class (default 200 per class per week) while still meeting `ρ*`.
- At `ρ* = 0.95`, one trace week becomes about 18k–28k jobs, depending on the week and seed
  (`results/workloads/summary.csv`).
- Thinning is deterministic for a seed: jobs are ordered by a seeded uniform draw, and the
  kept set is the prefix whose load is closest to `ρ*`. That is Bernoulli thinning with a
  solved `p`.

### 3.5 Scenarios

| Id | Purpose | Construction |
|---|---|---|
| `K` (calibration) | Calibrate the weights (§5.4) and anything tuned later. Kept separate from the evaluation weeks so nothing is tuned on the data it is judged on. | A typical trace week, thinned to `ρ*`. |
| `N` (natural) | Evaluation week with realistic, gently varying load. | A different typical week, thinned to `ρ*`. |
| `X` (shifting mix) | A week whose demand changes day by day, so a change of weights has something to respond to. | Seven one-day segments of real trace jobs, re-timed. Each segment keeps load near `ρ*` but changes who is asking: (1) natural mix; (2) small-short flood; (3) large-long heavy; (4) one account submitting about half the demand; (5) memory-heavy medium jobs; (6) natural mix; (7) alternating bursts of (2) and (3). |
| `M` (scale-out) | Longer horizon. Built only after one-week runs work, as the brief says: one week first, scale later. | Four consecutive weeks, and weeks from other months. |

How `X` is built (`config/scenarios.yaml`):
- **Source jobs:** the eligible weeks other than `K` and `N`.
- **Re-timing:** jobs keep their time of day, which preserves the daily cycle.
- **Load:** each segment, and each 6-hour block of the alternating day, is thinned to `ρ*` in
  the resource it saturates. The flood is CPU-bound; the large-long and memory-heavy days are
  memory-bound. So the week's load in either single resource comes out below `ρ*`: memory
  0.91–0.93 and CPU 0.66–0.71 across seeds.
- **Burst account:** the account with the most memory load in the pool (`account026`).
- **Size:** about 85k–88k jobs per week. Half of them are on the small-short flood day.

"Typical week": a Monday-to-Sunday window whose offered load, class mix and active-account
count are near the median of all full weeks in the trace. Weeks run from Monday 00:00 UTC,
which is 01:00 or 02:00 local time. Three kinds of week are not eligible:
- the holiday week of Jan 1;
- the maintenance week of Mar 11–17, when running jobs were killed on Mar 12;
- the last two weeks (from Jun 17). The files hold jobs by end time, so these weeks are missing
  jobs that ended after June 30.

Picked in Phase 1 from 22 eligible weeks (`results/profile/selection.json`):

| Scenario | Week (UTC) | Why |
|---|---|---|
| `N` | 2024-05-20 to 2024-05-26 | most typical eligible week |
| `K` | 2024-01-29 to 2024-02-04 | next most typical week at least two weeks from `N` |

### 3.6 Builder outputs
For each scenario and seed, under `workloads/traces/<scenario>-s<seed>/`:
- `jobs.parquet`: the table from §3.3.
- `events.txt`: the job submissions in the simulator's job-trace format (below).
- `accounts.txt`: the accounts, users and associations to create with `sacctmgr`.
- `manifest.json`: source window, filters, seed, `p`, target and achieved `ρ_cpu`/`ρ_mem`,
  per-class counts, and SHA-256 of each output.

Expected shape of one `events.txt` line, to be confirmed against the pinned simulator in
Phase 1. Only the events writer knows this format, so a mismatch is a one-file fix:

```
-dt <submit_s> -e submit_batch_job | -J job<id> -sim-walltime <runtime_s> --uid=<user> -A <account> -p normal -q normal -t <timelimit_min> -n <cpus> -N 1 --mem=<mem_mb> pseudo.job
```

Requirements: the same inputs and seed give byte-identical outputs. The builder reads
`dataset/` and never writes to it.

---

## 4. Job classification (Step 2)

### 4.1 Resource size
A job's **footprint** is the fraction of one node it occupies in its tighter dimension:

```
f = max(cpus / 32, mem_mb / 128000)
```

| Size | Footprint | Equivalent upper bound |
|---|---|---|
| Small | `f ≤ 1/16` | ≤ 2 cores and ≤ 8,000 MB |
| Medium | `1/16 < f ≤ 1/4` | ≤ 8 cores and ≤ 32,000 MB |
| Large | `f > 1/4` | up to a full node |

### 4.2 Time limit
Classes use the **requested** limit, since that is what the scheduler sees. Actual run time is
not used.

| Length | Time limit |
|---|---|
| Short | ≤ 4 h |
| Medium | > 4 h and ≤ 24 h |
| Long | > 24 h and ≤ 7 d |

### 4.3 The nine classes
The brief numbers the LARGE classes 4–6 again; this spec numbers them 7–9. Shares are for the
filtered January jobs.

| # | Class | Jobs | Core-hours | Memory-hours |
|---|---|---|---|---|
| 1 | `sm-short` | 39.9% | 5.2% | 2.8% |
| 2 | `sm-med` | 31.6% | 19.4% | 15.0% |
| 3 | `sm-long` | 14.4% | 23.7% | 16.4% |
| 4 | `med-short` | 1.7% | 0.2% | 0.2% |
| 5 | `med-med` | 1.9% | 1.0% | 1.3% |
| 6 | `med-long` | 9.1% | 27.4% | 35.2% |
| 7 | `lg-short` | 0.2% | 0.2% | 0.2% |
| 8 | `lg-med` | 0.6% | 2.7% | 4.0% |
| 9 | `lg-long` | 0.7% | 20.2% | 25.0% |

Most jobs are small, but most of the load is long. Thresholds live in `config/classes.yaml`,
not in code.

**Frozen in Phase 1.** All nine classes are populated in every month from January to June
(`results/profile/classes_by_month.csv`), so the thresholds stay as above. The mix moves a lot
from month to month: `sm-short` is 9–40% of jobs, and `lg-med` is 0.3–19%.

### 4.4 Rules
- Boundaries are inclusive on the upper side.
- Classification is deterministic and depends only on `cpus`, `mem_mb` and `timelimit`.
- Every job gets exactly one class. The class is stored in `jobs.parquet` and used in every
  per-class metric.

---

## 5. Priority weight configurations (Step 3)

### 5.1 Goals mapped to Slurm factors

| Goal | Brief's definition | Slurm factor | Why |
|---|---|---|---|
| **Fairness (F)** | No set of users gets placement priority over others | `PriorityWeightFairshare` | Fair-share lowers priority for accounts that used more than their share. |
| **Utilization (U)** | Use as much of each node as possible, CPU and memory | `PriorityWeightTRES=CPU=…,Mem=…` | The TRES factor grows with the requested share of CPU and memory, so big requests that fill nodes in both dimensions go first. |
| **Load balance (L)** | Keep utilization even across nodes | `PriorityWeightJobSize` with `PriorityFavorSmall=YES` and `SMALL_RELATIVE_TO_TIME` | Small, short jobs go first. They turn over fast and fill gaps on every node, instead of large long jobs pinning some nodes while others empty out. |

Notes:
- `PriorityFavorSmall` and `PriorityFlags` are global, so they are fixed in all configurations
  (§2.2). Only the weights change.
- Without `SMALL_RELATIVE_TO_TIME`, the job-size factor is nearly the mirror image of the CPU
  TRES factor, and U and L would just cancel. With the flag, L means "small **and** short
  first", which is a distinct behavior. Phase 1 checks the correlation of the two
  contributions on a real queue (§5.5).
- Placement stays at the Slurm default, so priority can affect load balance only through which
  jobs start next. Whether that is enough has to be measured in simulation, not assumed.

### 5.2 Tiers
Each configuration orders the three goals. The goal in tier 1 gets the largest priority swing,
tier 2 a middle one, and tier 3 the smallest:

| Tier | Target priority swing `T_k` |
|---|---|
| 1 (primary) | 4000 |
| 2 (secondary) | 2000 |
| 3 (tertiary) | 1000 |
| Age (all configs) | 500 at `PriorityMaxAge=1-0` |

Age is the same in every configuration. It is a tie-breaker and starvation guard, not a goal,
so it does not bias the comparison.

### 5.3 The six configurations
Numbering follows the brief. Together they cover all 3! orderings of {F, U, L}.

| Id | Ordering | Fairshare | TRES (CPU+Mem) | JobSize |
|---|---|---|---|---|
| C1 | F > U > L | tier 1 | tier 2 | tier 3 |
| C2 | F > L > U | tier 1 | tier 3 | tier 2 |
| C3 | L > F > U | tier 2 | tier 3 | tier 1 |
| C4 | L > U > F | tier 3 | tier 2 | tier 1 |
| C5 | U > F > L | tier 2 | tier 1 | tier 3 |
| C6 | U > L > F | tier 3 | tier 1 | tier 2 |

Baseline **B0 (FIFO):** all weights 0. Jobs are ordered by submit time, and backfill still
applies.

### 5.4 Weight calibration
Slurm normalizes each factor to 0–1, but the spread across real jobs is very different by
factor. Fair-share spans most of 0–1. The TRES factor is a request divided by the *partition*
total, so a full-node job scores only 1/16. Raw tier numbers would therefore let fair-share
swamp everything. Weights are scaled by each factor's observed spread:

```
s_g = P95(φ_g) − P5(φ_g)        # spread of factor g over pending jobs in calibration runs
W_g = round(T_tier(g) / s_g)
TRES split: W_cpu = round(0.5 · T / s_cpu),  W_mem = round(0.5 · T / s_mem)
```

TRES and job-size spreads come from the workload, because they are fixed per job. Fair-share
spread comes from the priority debug log (`DebugFlags=Priority`) of calibration runs. Calibration uses scenario `K` only.
The CPU/memory split defaults to 50/50. A CPU-leaning split is worth testing later because
memory is the bottleneck (§3.1).

### 5.5 Source of truth and verification
- `config/weights.yaml` holds the tiers, goal-to-factor map, spreads, split and the six
  orderings. A renderer turns it into `config/priority/<id>.conf` (B0, C1…C6), each containing
  only `PriorityWeight*` lines.
- Each rendered file loads in the simulator, and the priority debug log shows its weights.
  The simulator serves no CLI calls while it runs, so `sprio` is not available
  (`sim/README.md`).
- On a loaded queue, the priority debug log shows the primary goal's factor making the
  largest spread of priority contributions in each configuration.
- Pearson correlation between the U and L contributions over the pending queue is recorded. If
  `|r| > 0.8`, configurations that differ only in the order of U and L may behave alike; this
  is recorded as a finding.

