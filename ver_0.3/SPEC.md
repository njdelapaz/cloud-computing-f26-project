# SPEC — Adaptive Slurm Priority Weights (ver_0.3)

This document specifies Steps 1–6 of the `Project` brief in this folder:
- workloads, job classification and priority configurations (Steps 1–3, built in Phase 1);
- the cluster states that trigger a change and the mechanism that applies it (Steps 4–5,
  built in Phase 2);
- simulation of the full setup and its evaluation (Step 6, §8, approved for Phase 3);
- a revised strategy after the Phase 3 answer: B1 as the baseline, two favored states, and a
  stabilization period after each favored spell, set up on one-day workloads (§9, approved
  for Phase 4).

[PLAN.md](PLAN.md) lays out the order of work.

**Scope rule (from the brief):** everything lives in and refers only to `ver_0.3/`. All paths
in this document are relative to `ver_0.3/`.

---

## 1. Goal and research question

Build a mechanism that switches Slurm's multifactor priority weights while the cluster runs, and
simulate it to answer:

> Can switching priority weights based on the live cluster state adapt to changing workloads
> and provide better overall service than a fixed weight setting: the neutral configuration
> (C0) or a typical site setting (B1)?

What counts as "better" is defined in §8.1 and §8.6.

### Specified in this document
- Turning the trace into workloads that fit the simulated cluster (Step 1).
- A 9-class job classification (Step 2).
- Four priority configurations: favor one job size class (or none), then shortest time
  limit, then fairness (Step 3).
- The queue states that select a configuration (Step 4, §6).
- The mechanism that switches configurations while the simulation runs (Step 5, §7).
- Simulation of the full setup and its evaluation (Step 6, §8).
- The revised strategy and its one-day setup (§9).

### Out of scope
- Changing node selection or placement (`SelectType`, `LLN`, node weights). Only priority
  settings change.
- GPUs, multiple partitions, job dependencies, reservations, preemption. (The three size
  QOS of §5 only carry priority.)
- Changing what jobs request. Jobs keep their trace CPU, memory and time-limit requests.
- Running on a real cluster. Everything is tested in the UB Slurm Simulator. §7.4 describes
  how the same policy would run on a real cluster, but that path is not built or tested.

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
| QOS | `small`, `medium`, `large`: one per size class (§4.1), identical limits, used only to carry priority (§5). `normal` exists but is unused. |
| Scheduler | Slurm default (`SchedulerType=sched/backfill`, default backfill parameters) |
| Job limit | `MaxJobCount=200000`. Slurm's default of 10,000 rejected submissions on `X`'s flood day (task 3.4); 200,000 is above any workload's whole job count |
| Resource selection | `select/cons_tres` with `CR_Core_Memory`, so memory is a consumable resource |
| Priority plugin | `priority/multifactor` |
| Priority recalculation | `PriorityCalcPeriod=1` (every minute; tunable later) |
| Accounting | `slurmdbd`, needed for fair-share usage |
| Weekly capacity | 86,016 core-hours and 344,064 GB-hours |

### 2.2 Static `slurm.conf` priority keys

These stay fixed for a run. Only the weight keys and QOS priorities in §5 change. The one
exception is B1 (§8.2), a fixed reference setting for Step 6 with its own static keys.

```
PriorityType=priority/multifactor
PriorityCalcPeriod=1
PriorityDecayHalfLife=7-0          # Slurm default; tunable later
PriorityMaxAge=1-0                 # age factor saturates after one day
PriorityFavorSmall=NO              # with the flag below: shortest time limit first (§5.1)
PriorityFlags=SMALL_RELATIVE_TO_TIME
PriorityWeightPartition=0          # one partition, constant
Include priority_weights.conf      # PriorityWeight{Age,QOS,Fairshare,JobSize,TRES} of one configuration (§5)
```

### 2.3 Accounts and fair share
- One Slurm account for each trace `account`, and one user association for each
  (user, account) pair seen in the workload.
- Equal shares: every account has `RawShares=1` under `root`, and every user has `RawShares=1`
  within their account. The default fair-share algorithm (Fair Tree) is used.
- Fairness is considered at the **account** level first and the user level second.
- Every user may submit to every size QOS. Each configuration sets the three QOS priorities
  (`config/priority/<id>.qos`).

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
  matters for utilization, which is measured on cluster state (§5.5).

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
| `qos` | the size part of `class`: `sm` → `small`, `med` → `medium`, `lg` → `large` |
| `src_jobid`, `src_state` | provenance |

Every job's partition becomes `normal`; its QOS is its size class's QOS.

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
| `Y` (overlapping patterns) | Mixed, overlapping changes on an hour scale: a harder, more realistic test than `X`'s one-pattern days. | Natural background at 60% of `ρ*` all week, plus 22 bursts of `X`'s patterns (small-short flood, large-long, one-account, memory-heavy medium). Each burst lasts 3–12 h, starts at a seeded random hour and adds 40% of `ρ*` while it runs. |
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

How `Y` is built (`config/scenarios.yaml`, `overlay`):
- **Source and timing:** the same source jobs and time-of-day placement as `X`.
- **Background:** thinned to 60% of `ρ*` on each day.
- **Bursts:** the four patterns are used in turn, in a seeded order, so each appears 5–6 times
  a week. Every burst keeps at least one job, because one multi-day large job can exceed a
  short burst's whole target.
- **Coverage:** start times are random, so some hours have no burst and some have two or more.
  Seed 1 has 78 h with none, 41 h with one and 49 h with two or more.
- **Load:** the week's memory load lands at 0.88–0.96 across seeds, and peaks run above `ρ*`
  where bursts overlap.
- **Size:** about 47k–56k jobs per week. Burst details are in
  `results/workloads/Y_bursts.csv`.

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

One `events.txt` line (format confirmed against the pinned simulator in Phase 1). Job names
run from `jobid_1001` in submission order, matching Slurm's job ids:

```
-dt <submit_s> -e submit_batch_job | -J jobid_<id> -sim-walltime <runtime_s> --uid=<user> -t <timelimit_min> -n <cpus> --ntasks-per-node=<cpus> -N 1 --mem=<mem_mb> -A <account> -p normal -q <qos> pseudo.job
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

## 5. Priority configurations (Step 3)

The brief lists six orderings of three goals: fairness, utilization and load balance. On
2026-11-26 they were replaced, on request, by configurations built for four queue scenarios.
This section supersedes the brief's WEIGHT CONFIGURATIONS list.

| Queue scenario | Configuration |
|---|---|
| Many large jobs waiting | C1: favor large |
| Many small jobs waiting | C2: favor small |
| Many medium jobs waiting | C3: favor medium |
| A mix | C0: neutral |

Every configuration orders waiting jobs by **size class preference, then shortest time
limit, then fairness**. How the scenarios are detected is part of Step 4 (PLAN 2.1).

Utilization and load balance are no longer priority factors. Both are measured on cluster
state (§5.5). Phase 1 showed why priority cannot carry them:
- Slurm's size factors only ever favor bigger, or only ever favor smaller, jobs.
- Priority barely changes how evenly the nodes are used, because Slurm's default placement
  fills the low-numbered nodes first.

### 5.1 Slurm factors

| Criterion | Slurm factor | How it is computed |
|---|---|---|
| Size class | QOS (`PriorityWeightQOS`) | Every job runs in the QOS of its size class: `small`, `medium` or `large` (§4.1, §3.3). The three QOS have identical limits. A configuration sets their priorities, and Slurm's factor is the QOS priority divided by the largest QOS priority. |
| Shortest time limit | Job size (`PriorityWeightJobSize`) with `SMALL_RELATIVE_TO_TIME` and `PriorityFavorSmall=NO` | `max(32, cpus) / T_min / 512`. Every single-node job counts as at least one node's average CPUs (32), so only the time limit matters, and shorter limits score higher. |
| Fairness | Fair-share (`PriorityWeightFairshare`) | Fair Tree with equal shares (§2.3). |
| Tie-breaker | Age (`PriorityWeightAge=500`) | Same in every configuration; saturates after 1 day. |

TRES weights are 0.

**Why one QOS per size class.** Slurm's own size factors (TRES, and job size without the
flag) are monotonic, so none of them can favor *medium* jobs. A label QOS per class gives any
class preference with built-in factors only. Alternatives considered:
- A custom site-factor plugin (C code in the simulator).
- One partition per class, which would change scheduling itself.
- TRES weights, which can only favor large jobs.

**Job-size factor check** (pinned source and simulator logs). Computed factors match Slurm's
logged values to 5×10⁻⁷. `PriorityFavorSmall=YES` would turn the factor into `1 − x` and favor
*long* jobs; the first draft of this spec had that wrong.

### 5.2 Tiers

| Criterion | Target | Weight |
|---|---|---|
| Size class | **Strict**: one step between neighbouring classes outweighs everything else combined | `W_QOS = ⌈(W_JobSize · 0.0625 + W_Fairshare + W_Age + 1) / step⌉` |
| Shortest time limit | Typical gap of 2,000 points between two waiting jobs of the same class | `W_JobSize = 2000 / s_time` |
| Fairness | Typical gap of 1,000 points between two waiting jobs of the same class | `W_Fairshare = 1000 / s_fs` |
| Age | 500 at most | `PriorityWeightAge=500`, `PriorityMaxAge=1-0` |

- **The strict bound.** `step` is the gap between neighbouring normalized QOS priorities: 0.5
  for graded configurations, 1.0 for favor medium. `0.0625` is the largest job-size factor,
  reached by a 1-minute limit. So the favored class always comes first.
- **Strict was chosen 2026-11-26.** With soft tiers (4,000 points per step), short limits
  routinely outweighed the size preference. The time factor grows as `1/T`, so a 30-minute
  job scores about 227,000 points more than a 1-day job. The favored class then led in only
  77–89% of cross-class pairs.
- **Within a class the tiers stay soft.** A very short limit can outrank a fairer job.

### 5.3 The configurations

| Id | Name | QOS priority (small / medium / large) | Normalized QOS factor |
|---|---|---|---|
| C0 | neutral (mix) | 0 / 0 / 0 | 0 / 0 / 0 |
| C1 | favor large | 0 / 50 / 100 | 0 / 0.5 / 1 |
| C2 | favor small | 100 / 50 / 0 | 1 / 0.5 / 0 |
| C3 | favor medium | 0 / 100 / 0 | 0 / 1 / 0 |

- C1 and C2 are graded: favoring large also puts medium ahead of small.
- Favor medium treats small and large equally.
- Baseline **B0 (FIFO)** has all weights 0 and all QOS priorities 0. Jobs go in submit order,
  and backfill still applies.
- Each configuration is two files: `config/priority/<id>.conf` (weights) and `<id>.qos` (QOS
  priorities). `sim/weights.py render` writes both from `config/weights.yaml` and
  `config/factor_scales.yaml`.

### 5.4 Weight calibration

Slurm normalizes each factor to 0–1, but their spreads among real jobs differ enormously. The
time and fairness weights are therefore scaled by each factor's typical gap between two
waiting jobs: its Gini mean difference.

```
s_g = median over (hourly snapshot, size class) of  mean |φ_g(i) − φ_g(j)|
      over waiting jobs i ≠ j of the same class
```

- **Waiting jobs, not the whole workload.** Priority only orders the queue, and the queue is
  not a random sample of the workload. Large jobs pile up, and short-limit jobs start quickly.
- **Within a class.** With size strictly first, time limit and fairness only order jobs of
  the same class.
- **Per configuration.** Each configuration reshapes its own queue: favoring short limits
  leaves the queue with jobs whose limits are alike. So each configuration's scales come from
  its own check run.
- **Gini mean difference, not P95 − P5.** A first draft used P95 − P5, but over 95% of jobs
  ask for 1 core, which made that spread 0 for the CPU factor.

**Procedure** (`sim/check_configs.py` runs B0 and every configuration on the first 36 h of
`K-s1` with `DebugFlags=Priority`; `sim/weights.py calibrate` measures the scales):

| Pass | Design | Scales from | Outcome |
|---|---|---|---|
| v3a | soft size; then fairness, then time | pooled waiting jobs of an earlier check | favored class first in 66–93% of cross-class pairs (fairness overrides) |
| v3b | soft size; then time, then fairness | same | 77–89% (short limits override) |
| v3c | **strict size**; then time, then fairness | each configuration's own v3b queue, within classes | see §5.5 |

Earlier designs went through the same steps:
- the six three-goal orderings (passes 1–3);
- the four two-goal pairings (v2a).

Their results are in `results/check/`.

**Weights used in v3c** (`config/priority/*.conf`):

| Id | QOS | Job size (time) | Fair-share | Age |
|---|---|---|---|---|
| C0 | 0 | 268,565,244 | 6,174 | 500 |
| C1 | 32,981,060 | 263,726,990 | 7,092 | 500 |
| C2 | 27,420,378 | 219,272,039 | 5,185 | 500 |
| C3 | 16,431,432 | 262,807,945 | 5,434 | 500 |

The largest possible priority is about 0.3 billion, inside Slurm's 32-bit limit.

### 5.5 Verification

Per configuration, over hourly snapshots of the waiting queue in the check runs:
- Slurm's logged weights equal the rendered file, and every job's QOS factor equals its
  class's configured value.
- **Size first:** in every pair of waiting jobs from classes with different QOS factors, the
  more favored job has the higher priority (strict: all pairs).
- **Then time, then fairness:** among waiting jobs of the same class, the typical time-limit
  gap exceeds the typical fair-share gap.
- **Cluster state over the window**, reported for comparison and not a pass criterion:
  - node imbalance: the standard deviation across the 16 nodes of per-node utilization
    `(CPU + memory) / 2`. **Load balance is measured on the cluster, not the queue.**
  - utilization;
  - mean wait.

**Result (pass v3c, `results/check/configs_v3c.json`).** In all five runs, every job ran as
specified, the logged weights match, and every QOS factor is correct.

| Id | Favored class first | Within class: time / fairness gap | Node imbalance σ | Utilization | Mean wait |
|---|---|---|---|---|---|
| B0 | – | – | 0.210 | 0.61 | 72 min |
| C0 | – | 1,976 / 1,137 | 0.192 | 0.62 | 17 min |
| C1 | 100% | 1,966 / 929 | 0.195 | 0.62 | 24 min |
| C2 | 100% | 2,335 / 1,152 | 0.202 | 0.61 | 13 min |
| C3 | 100% | 1,915 / 1,001 | 0.210 | 0.61 | 13 min |

All four configurations pass.

These cluster numbers come from single 36-hour runs, and the simulator varies from run to
run. Two FIFO runs of the same slice gave mean waits of 88 and 51 minutes. So they show
direction, not effect sizes; Step 6 needs several runs per configuration. Node imbalance
barely moves under any configuration (σ 0.19–0.21), because Slurm's default placement fills
`n01` and `n02` (85–89% busy) long before `n07`–`n10` (40–50%).

---

## 6. Queue states that select a configuration (Step 4)

### 6.1 States and configurations

At regular simulated times (**ticks**), the controller reads the waiting queue, decides which
of four states it is in, and selects that state's configuration (§5.3):

| State | Meaning | Configuration |
|---|---|---|
| `large` | Unusually many large jobs waiting | C1: favor large |
| `small` | Unusually many small jobs waiting | C2: favor small |
| `medium` | Unusually many medium jobs waiting | C3: favor medium |
| `mix` | No class stands out | C0: neutral |

### 6.2 What is measured

At each tick, the waiting jobs (pending and eligible) are counted by size class:
`n_small`, `n_medium`, `n_large`, with total `n`. The signal is each class's **share of the
waiting jobs**, `s_c = n_c / n`.

A share alone cannot be compared across classes, because the classes have very different
normal levels. Over the FIFO run of the full `K-s1` week (ticks every 10 minutes with at
least 20 jobs waiting):

| Class | Median share | 10th–90th percentile |
|---|---|---|
| small | 0.90 | 0.74–0.94 |
| medium | 0.08 | 0.05–0.20 |
| large | 0.01 | 0.00–0.05 |

So small jobs are always the majority, and a fixed share threshold would always pick
`small`. A ratio to the median would fire for `large` on tiny changes (1% to 2%) and never
for `small` (it cannot pass 1.11×). In that week, a ratio rule at 1.5× would put `large` in
27% of ticks and `small` in none.

### 6.3 Normalization: each class against its own range

Each share is converted to a **percentile rank** `P_c` within that class's own distribution
in a calibration run. A class is unusually present when its share is high *for that class*:
- small at 0.95 might be its 95th percentile;
- large at 0.05 its 90th.

That puts the three classes on one scale.

**Calibration run:** the full `K-s1` week under C0 (neutral), so no class is favored, with
ticks every 10 minutes and at least 20 jobs waiting. The percentile tables go to
`config/triggers.yaml`. Calibration uses scenario `K` only.

### 6.4 Decision rule

At each tick:

1. **Gate.** If fewer than `n_min = 20` jobs are waiting, keep the current configuration.
   Priority hardly matters with a short queue.
2. **Candidate.** Let `c*` be the class with the highest `P_c`.
   - If `P_c* ≥ P_enter` (80), the candidate state is `c*`.
   - Otherwise the candidate state is `mix`.
3. **Hysteresis.** Leave the current state only if all of these hold:
   - the candidate has been the same for `k = 2` consecutive ticks;
   - the current state has lasted its minimum stay `T_dwell` (time since the last switch;
     60 minutes by default, set for every state at once or per state, §6.6);
   - when the current state is a class `c`, its `P_c` has dropped below `P_exit` (60), or
     another class has reached `P_enter` with a higher percentile than `c`.
4. **Output.** The state's configuration (§6.1), and whether it changed.

The controller starts in `mix` (C0). Every tick is logged with its inputs and decision
(§7.5).

### 6.5 Feedback

The active configuration changes the signal it reads. Favoring a class makes its jobs start
sooner, which lowers that class's share of the waiting queue and can end the state that
selected it. Hysteresis and the dwell time limit how fast this can flip.

Step 6 will report switch rates and reversals. If they are high, the alternative signal in
§6.6 (Q-A) is the first thing to try.

**Measured in Phase 2** (the adaptive `K-s1` week, task 2.6): the feedback is real.
- Over the 7 submission days the controller switched 46 times (6.6 a day). Replaying the
  policy over the neutral week had predicted 4.3 a day.
- 13 switches (28%) returned to the previous state within 2 hours, and 21 of the 45 gaps
  between switches were at the 60-minute minimum.
- Time in each state: mix 48%, small 24%, large 22%, medium 6%.

The flapping comes from small and medium. Every time either was left, it was because its own
share had fallen below `P_exit`:

| State | Stays | Median stay | Left at the 60-min minimum | Reversed within 2 h |
|---|---|---|---|---|
| small | 16 | 80 min | 8 | 10 |
| medium | 9 | 60 min | 8 | 2 |
| large | 9 | 160 min | 2 | 5 |
| mix | 11 | 140 min | 3 | 5 |

Large jobs start slowly, so favoring them does not empty the queue as fast. Large usually
ended because another class overtook it. This is why the minimum stay can be set per state.

Replaying the policy offline over the queue this run saw (open loop: it cannot show how each
switch would change the queue) gives:

| Minimum stay | Switches a day | Time in mix / small / medium / large |
|---|---|---|
| 60 min for every state (this run) | 6.6 | 48 / 24 / 6 / 22% |
| 120 min for small and medium, 60 for large and mix | 4.9 | 43 / 26 / 8 / 22% |
| 120 min for every state | 4.3 | 45 / 25 / 7 / 24% |

Whether fewer switches gives shorter waits needs closed-loop runs (Step 6).

### 6.6 Parameters and open questions

| Parameter | Default | Where |
|---|---|---|
| Tick interval `Δ` | 10 simulated minutes | `config/triggers.yaml` |
| Minimum waiting jobs `n_min` | 20 | same |
| Enter / exit percentile | 80 / 60 | same |
| Persistence `k` | 2 ticks | same |
| Minimum stay `T_dwell` | 60 minutes for every state; `dwell_s` may instead be a table with one value per state (`mix`, `small`, `medium`, `large`) | same |
| Calibration | `K-s1` week under C0 | same |

**Open questions** (defaults approved 2026-11-26):

| # | Question | Default |
|---|---|---|
| Q-A | Signal: the waiting queue, as framed by the queue scenarios, or recent **arrivals** by class (last hour), which the active configuration cannot affect | Waiting queue |
| Q-B | Count jobs ("number of jobs"), or weigh them by requested resources × time limit | Count |
| Q-C | Percentile thresholds and dwell | 80 / 60, 60 minutes |

---

## 7. Configuration change mechanism (Step 5)

### 7.1 Constraint from Phase 1

The simulator never starts `slurmctld`'s RPC thread. So while a simulation runs, no Slurm
command can read state or reconfigure the controller (`sim/README.md`). Simulated time also
exists only inside the controller, as a clock that jumps from event to event.

The mechanism therefore runs **inside the simulator's event loop**, as a small patch to the
pinned simulator source. The decision logic stays outside, in Python, so it can be tested
and replayed on its own.

### 7.2 Components

```
 slurmctld (simulated)                                     controller/ (Python)
 ┌──────────────────────────────────────────┐   state.json  ┌───────────────────────┐
 │ SIM_POLICY_TICK every Δ                   ├──────────────▶│ policy.decide(state,  │
 │   1. write snapshot                       │               │   memory, params)     │
 │   2. run PolicyCommand (blocking)         │◀──────────────┤ → decision.json       │
 │   3. if switch: apply configuration       │ decision.json │ (pure function; keeps │
 │      - priority weights → reconfig        │               │  hysteresis memory in │
 │      - QOS priorities → assoc_mgr update  │               │  a small state file)  │
 │      - recompute priorities now           │               └───────────────────────┘
 │   4. log the tick                         │
 └──────────────────────────────────────────┘
```

| Component | Where | Responsibility |
|---|---|---|
| Policy tick | patch to `contribs/sim/` (new event `SIM_POLICY_TICK`) | Fires every `Δ` simulated seconds from the first job's submission, and runs steps 1–4. |
| Snapshot | patch | Writes `state.json`: simulated time, active configuration, jobs submitted so far per QOS (§8.3), and for each waiting job its id, QOS, CPUs, memory, time limit, account and time waited; plus allocated CPU and memory per node. |
| Policy | `controller/policy.py` | A pure function of the snapshot, its own small memory (current state, candidate streak, last switch time) and the parameters. Returns the configuration and the reason. No I/O. |
| Command | `controller/tick.py`, run as `PolicyCommand` | Reads the snapshot and memory, calls the policy, writes `decision.json` and the updated memory, and appends to the controller log. |
| Apply | patch | Sets `slurm_conf.priority_weight_*` from the configuration's `.conf` file and calls the priority plugin's reconfigure entry. It applies the three QOS priorities from the `.qos` file through the accounting manager's QOS update, which also recomputes Slurm's normalized QOS priorities. Then it schedules an immediate priority recalculation. |
| Scheduled switch | patch to the events-file parser | A new event `-dt <s> -e set_priority_config \| <id>` applies a configuration at a fixed time through the same apply step. Used to test the apply step and for fixed schedules. |

Settings go in the run's `sim.conf`: `PolicyTickSeconds`, `PolicyCommand`, `PolicyDir`,
`InitialConfig`. With no `PolicyCommand`, a run behaves exactly as today. Static runs and
the Phase 1 checks are unchanged.

### 7.3 Behaviour

- **Latency.** A switch takes effect in the same simulated minute: priorities are
  recalculated right after it is applied.
- **Scope.** Only the five `PriorityWeight*` values and the three size-QOS priorities change.
  Jobs, nodes, partitions and the queue are never touched.
- **Failure.** If the command fails or its output is invalid, the configuration stays as it
  is, the failure is logged, and the next tick tries again. There is at most one switch per
  tick.
- **Time cost.** The command runs in real time while the simulated clock follows the real
  clock between events, so each tick lets simulated time move by the command's run time
  (tens of milliseconds). This is logged and expected to be negligible.
- **Reproducibility.** The policy is deterministic. The same snapshots give the same
  decisions offline (replay), which is how online runs are checked.

### 7.4 On a real cluster (specified, not built or tested)

The same `controller/policy.py` would read state from `squeue --json` and `sinfo --json`.
It would apply a configuration by:
- rewriting the `Include`d weight file and running `scontrol reconfigure`;
- running `sacctmgr -i modify qos <name> set priority=<p>` for the three QOS.

A `dry-run` mode would log decisions without applying them.

### 7.5 Controller log (one JSON line per tick)

```json
{"t_sim": "2024-01-30T10:20:00", "waiting": 412,
 "counts": {"small": 360, "medium": 31, "large": 21},
 "shares": {"small": 0.874, "medium": 0.075, "large": 0.051},
 "percentiles": {"small": 41, "medium": 52, "large": 91},
 "state": "mix", "candidate": "large", "streak": 2, "minutes_since_switch": 130,
 "decision": "switch", "config": "C1", "reason": "large at P91 >= 80 for 2 ticks",
 "command_ms": 38}
```

The simulator also logs each applied switch in `slurmctld.log`
(`policy: switch C0 -> C1 at <time>`).

### 7.6 How Phase 2 is verified

1. **Policy unit tests.** Synthetic snapshots for each state, the gate, the persistence and
   dwell rules, and exit by percentile drop.
2. **Apply test.** Scheduled switches C0 → C1 → C2 → C3 → C0 on a `K` slice with
   `DebugFlags=Priority`. Within one simulated minute of each switch, the logged weights and
   QOS factors must equal the new configuration's, and every job must run as specified.
3. **Replay.** Decisions recomputed offline from the logged snapshots must equal the online
   decisions.
4. **End-to-end week.** An adaptive run of the full `K-s1` week finishes, with every tick
   logged and the run check passing.

**Result (Phase 2).** All four pass:
1. The policy unit tests pass.
2. The apply test passes: every switch recomputed all waiting jobs within 2 simulated
   seconds with the new weights and QOS factors. A missing configuration was rejected, and a
   failing command was logged without a switch.
3. Replay reproduced all 1,981 decisions of the adaptive week.
4. The adaptive week finished in 1 h 14 min real time:
   - 1,981 ticks logged on both sides, with no command failures;
   - all 49 requested switches applied, in order;
   - every job ran as specified;
   - each policy command took about 34 ms.

Its mean wait was 84 minutes (neutral C0 week: 120; FIFO: 549). These are single runs, so
they show direction only.

Files: `results/phase2/apply_test.json` and `results/phase2/K-s1-adaptive_summary.json`.

---

## 8. Simulation and evaluation (Step 6)

*Approved 2026-11-26, with the default answers to open questions E-A to E-E (§8.9).*

### 8.1 What "better" means

Step 6 runs the full setup (§3–§7) on weeks it was not tuned on and answers §1's question.
**Better overall service** means shorter waits for the workload as a whole, without making
any size class, utilization or fairness across accounts noticeably worse. §8.6 turns this
into pass/fail rules.

Everything the policies use was fixed on `K`: weights (§5.4), percentile tables (§6.3) and
minimum stays (§6.5). B1 is taken as published, with no tuning at all. Evaluation uses only
`N`, `X` and `Y` (§3.5), and nothing is tuned on them.

### 8.2 What is compared

| Id | Kind | Setting |
|---|---|---|
| B1 | static | A typical site setting: SchedMD's multifactor example, fair share first (below) |
| C0 | static | neutral (§5.3) |
| A1 | adaptive | The Phase 2 trigger: waiting-queue shares, 60-minute minimum stay in every state (§6) |
| A2 | adaptive | A1 with minimum stays of 120 min for small and medium, 60 min for large and mix (§6.5) |
| A3 | adaptive | A1 with the arrivals signal (§8.3) in place of the waiting queue |

All adaptive runs start in `mix` (C0) and use the same thresholds, persistence and tick.

**Fixed settings compared (changed 2026-11-27, requested).** Only C0 and B1. FIFO (B0) and the
single-class configurations C1–C3 run fixed are not compared: C1–C3 exist to be switched to,
and C0 and B1 are the fixed settings a site would choose between. Runs of B0 and C1–C3 made
before the change (all of `N`, and `Y-s1`) are kept but left out of the comparison. On
`N-s1`, C2 run fixed had the lowest mean wait of all nine (65 min, against C0's 75 and the
adaptive variants' 88–111), which the report notes.

**B1, the typical site setting.** The configuration example on Slurm's multifactor priority
page (`priority_multifactor.html`), used as published:

```
PriorityDecayHalfLife=14-0
PriorityMaxAge=14-0
PriorityFavorSmall=NO              # no SMALL_RELATIVE_TO_TIME: larger requests score higher
PriorityWeightFairshare=10000
PriorityWeightAge=1000
PriorityWeightJobSize=1000
PriorityWeightPartition=1000       # one partition: the same for every job
PriorityWeightQOS=0
```

- **Kept from §2.2:** `PriorityCalcPeriod=1` (the brief) and TRES weights 0 (the example sets
  none). Jobs keep their size QOS, which has no effect with a QOS weight of 0.
- **Static keys:** B1 never switches, so its runs can differ from §2.2. Its keys replace the
  template's lines; they are not added after them.
- **Expected order:** with the factor gaps measured in §5.4 (about 0.17 in fair share between
  two waiting jobs), fair share decides most of B1's order. Age and job size act as
  tie-breakers.

**The bar.** `S*` is whichever of C0 and B1 has the lower primary metric averaged over all
nine evaluation workloads: the one of the two an operator would pick to use everywhere. The
better of the two on each workload, chosen with hindsight, is also reported.

### 8.3 Arrivals signal (A3)

§6.5 showed the waiting queue answers back: favoring a class empties it, and the state ends.
Arrivals do not depend on the configuration, so they cannot feed back.

- **Signal.** At each tick, each size class's share of the jobs submitted in the last 60
  simulated minutes.
- **Normalization.** As in §6.3, a percentile against that class's own range, taken from the
  `K-s1` workload itself every 10 minutes. No simulation is needed, because arrivals are
  the same under every configuration.
- **Gate.** Hold when fewer than `n_min` (20) jobs are waiting, or fewer than 20 jobs arrived
  in the window.
- **Snapshot.** Finished jobs leave `slurmctld`'s job list after `MinJobAge` (300 s), so
  arrivals cannot be read from it. The hook counts each job by its size QOS (the `-q`
  argument) when the simulator submits it, and `state.json` gains the running totals,
  `"submitted": {"small": n, "medium": n, "large": n}`. The controller keeps the totals of
  recent ticks and takes the difference over the window. This is a change to
  `sim/patches/policy_hook.patch`, so the image is rebuilt before any evaluation run.
- **Check.** At every tick of an A3 run, the logged totals must match the run's sacct submit
  times, and the arrivals must be the totals' difference over the window.

**Calibration (task 3.3, `config/triggers_A3.yaml`, `results/phase3/arrival_calibration.json`).**
Over the `K-s1` week, 981 of 1,008 ticks had at least 20 arrivals in the previous hour (median
145). Median shares: small 0.92, medium 0.07, large 0.00. Large jobs arrive one to five an
hour, so its share is 0 in most windows, and one or two large arrivals put it near the top of
its range. A replay over the week (arrivals for the signal, the C0 run's queue for the gate)
spends 36% of the time in large, 27% in small, 21% in mix and 17% in medium, with 7.9
switches a day. Arrivals do not answer back, so runs should see about the same rate.
Following risk R10, this is reported, not tuned.

**Check result (task 3.3, `results/phase3/image_checks.json`).** On a 24-hour `K-s1` slice,
all 770 ticks matched sacct and the window differences, and replay was exact.

### 8.4 Runs

| Item | Value |
|---|---|
| Workloads | `N`, `X`, `Y` × seeds 1–3: nine workloads |
| Policies | the five in §8.2 |
| Matrix | 5 × 9 = 45 one-week runs |
| Pilot | C0 and A1 on seed 1 of each scenario, run alone: 6 runs |
| Repeat runs | C0 and A1 on seed 1 of each scenario, three runs each: the pilot run, the matrix run and one more, so 6 more runs |
| Total | 57 runs (plus 16 runs of the dropped fixed settings made before the change: B0 and C1–C3 on N-s1–N-s3 and Y-s1) |

- **Same build for every run.** One image (after the A3 change) and the same settings:
  priority every minute, ticks every 10 minutes, no `DebugFlags=Priority` (its log runs to
  gigabytes a week; ordering was verified in §5.5 and §7.6).
- **Each run drains.** Submissions span the 7 days and the run continues until every job has
  ended, as in Phase 2 (`K` took about 14 simulated days).
- **Batches.** The policies of one workload run together as one batch, so host load
  affects them alike. The pilot shows whether the host carries nine at once (memory, CPU)
  and how long `X` and `Y` take. `K` took 1 h 14 min to 1 h 19 min real time per run; `X`
  and `Y` have 2–3.4 times as many jobs.
- **Checks.** Every run must pass `sim/check_run.py` (known simulator issues are reported,
  not failures). Adaptive runs must also pass `controller/summarize.py`: every tick logged on
  both sides, every switch applied, replay exact. A run that fails is rerun once. If it fails
  again, it is excluded and the report says so.

### 8.5 Metrics

Every job of a workload is submitted in its 7-day week, and every run drains, so each job has
a wait (start − submit) and a run time.

**Service**
- **Mean wait**: the primary metric (open question E-A).
- Median and 95th-percentile wait.
- Mean bounded slowdown, `(wait + run) / max(run, 10 min)`.
- Mean and 95th-percentile wait for each size class (small, medium, large), plus mean wait
  for each of the nine classes.

**Cluster state**, from 5-minute samples over the 7-day submission window (§5.5):
- Utilization: mean allocated `(CPU + memory) / 2`, and each separately.
- Node imbalance σ (`sim/runstats.py`, `cluster_balance`). Reported only: priority does not
  change placement (§1), and in Phase 1 σ barely moved under any configuration.
- Makespan: last end minus first submit.

**Fairness**
- Jain's index `(Σx)² / (n·Σx²)` over accounts, where `x` is an account's mean bounded
  slowdown. Accounts with at least 20 jobs. 1 means every account fares the same.

**Adaptation** (adaptive runs)
- Switches a day, reversals within 2 hours, time in each state, and stays at the minimum
  (`controller/summarize.py`).
- `X`: the state over the week against the day patterns, and each policy's mean wait for
  the jobs of each day. Does the adaptive policy track the best static setting day by day?
- `Y`: mean wait of burst jobs and of background jobs (jobs carry their `segment`).
- Policy command time.

### 8.6 Analysis and pass rules

**Paired comparisons.** Policies are compared on the same workload. For each workload, the
result is each adaptive variant's metric against `S*`'s.

**Noise band.** For scenario `s`, the band `b_s` is the larger relative range,
`(max − min) / mean`, of the primary metric over the three C0 runs and over the three A1
runs of seed 1. A difference counts only if it is larger than `b_s` times `S*`'s value.

**Effective on a scenario.** An adaptive variant is effective on `s` when, on all three seeds:
1. its mean wait is lower than `S*`'s by more than the noise band; and
2. these guards hold against `S*` on the same workload (open question E-D):
   - no size class's mean wait is more than 25% higher;
   - utilization is no more than 1 percentage point lower;
   - Jain's index is no more than 0.05 lower.

**Answer to §1.**
- **Yes:** an adaptive variant is effective on both `X` and `Y`, and on `N` it is within the
  noise band of `S*` or better, with the guards holding.
- **Partly:** effective on only one of `X` and `Y`, or effective on both but worse than `S*`
  on `N`.
- **No:** otherwise.

All three variants are reported, not only the best. Picking the best of three on the same
weeks favors it slightly. The scale-up (§8.7) uses new weeks, so it checks that choice.

**Result (task 3.6; `results/phase3/comparison.json`, `results/phase3/fig_*.png`).**
The answer is **no**.

- **The bar:** `S*` is C0, at a mean wait of 142 min over the nine workloads against B1's
  280. C0 is the better fixed setting on 8 of 9 workloads; B1 wins only `X-s2`.
- **Noise bands:** 0.22 on `N`, 0.31 on `X`, 0.24 on `Y`. In all six repeat sets the
  third run, made alongside the `X-s3` batch, came out higher than the other two (by up to
  31% on `X`). The number of runs sharing the host does not explain it, so the bands are
  conservative.
- **No adaptive variant is effective on any scenario.** Of the 27 comparisons with `S*`:
  - 26 are worse than C0 (15 beyond the noise band) and 1 is better;
  - the only win beyond noise, A3 on `N-s2` (−25%), fails the small-job guard;
  - the only run within noise with every guard holding is A3 on `X-s1` (+7%);
  - the small-job guard fails in 22 of 27, utilization in 13, fairness in none.

Mean wait in minutes, mean of the three seeds:

| Scenario | C0 | B1 | A1 | A2 | A3 |
|---|---|---|---|---|---|
| `N` | **73** | 154 | 94 | 83 | 82 |
| `X` | **321** | 547 | 412 | 417 | 597 |
| `Y` | **33** | 139 | 55 | 60 | 189 |

What the adaptive policies do:
- **They move waiting from medium and large jobs onto small jobs.** On `N`, medium jobs wait
  1.6–4.3 h instead of 6.7 h and large jobs about 15 h instead of 19.5 h (A1, A2), while
  small jobs wait 55–63 min instead of 25. Small jobs are about 90% of all jobs, so the mean
  rises.
- **On `X` they help on the days built for them and lose elsewhere.** They gain on the
  large-long and memory-heavy days and lose on the natural and one-account days
  (`fig_x_days.png`).
- **A3 is the weakest on `X` and `Y`.** One or two large arrivals in an hour make large look
  unusual, so it favors large for long stretches (61% of the `Y` week) and small jobs wait.
- **B1 is erratic on `X`:** 54 min on `X-s2` but 900 min on `X-s3`. Fair share first does not
  protect short jobs, and under `X`'s near-saturated load its outcome swings with the seed.

**While a class is favored (`evaluation/favored.py`, `results/phase3/favored.json`).** Each
job's wait is split across the policy's states. Jobs that spent most of their wait while their
own class was favored are compared with the same jobs in the C0 run of the same workload.
Means are over the three seeds:

| Favored class | `N` (A1 / A2 / A3) | `X` | `Y` |
|---|---|---|---|
| medium | −55 / −65 / −91% | +5 / −11 / −32% | −79 / −80 / −84% |
| large | −33 / −30 / −83% | +57 / +15 / −51% | −1 / −4 / −30% |
| small | +103 / +49 / +26% | +17 / +25 / +31% | +113 / +145 / +368% |

- **Favoring medium works.** Medium jobs wait far less, and on `N` the other jobs' extra
  waiting about cancels it out (A3: a net saving).
- **Favoring large works on `N`, and for A3 elsewhere.** On `N` the other jobs lose more than
  the large jobs gain (A1: 4,300 job-hours saved, 11,500 lost). A3's long large spells on
  `X` and `Y` cost others 230,000–750,000 job-hours.
- **Favoring small never helps small jobs.** C0 already starts short jobs first, and most
  small jobs are short. The small state also starts when a small backlog has built up, often
  after the policy favored large or medium. Small-state periods carry most of the adaptive
  policies' extra waiting.

The comparison uses a separate C0 run, so it carries run-to-run noise. It also folds in the
policy's own history: a backlog built up under one state is waited out under the next.

### 8.7 Scale-up (`M`)

*Not run: skipped on request (2026-11-27) after the one-week answer was no. `M` was not
built.*

The brief says one week first, scale later. Once the one-week answer is in:
- **`M`:** four consecutive eligible weeks, weeks 11–14 (2024-03-18 to 2024-04-14), thinned
  to `ρ*` like `K` and `N`, seeds 1–3.
- **Policies:** C0, B1 and the best adaptive variant from §8.6. A1 is added if it is not
  the best. That is 9–12 runs, each about four times as long as a week.
- **Questions:**
  - Does the one-week result hold over a month?
  - Do `K`'s percentile tables still fit other months? Under C0, the share of busy ticks at
    or above P80 for each class is about 20% by construction in `K`; report it for each
    week of `M`.

### 8.8 Outputs

- `results/phase3/runs/<scenario>-s<seed>/<policy>/`: raw runs (gitignored).
- `results/phase3/metrics.csv`: one row per run, with every §8.5 metric.
- `results/phase3/comparison.json`: noise bands, `S*`, paired differences, the pass rules
  per scenario and the answer.
- `REPORT.md`: the answer, tables and figures, and limitations:
  - the simulator varies from run to run;
  - placement is fixed;
  - one trace;
  - the rare lost finish event (`sim/README.md`).

### 8.9 Open questions

Defaults approved 2026-11-26.

| # | Question | Default |
|---|---|---|
| E-A | Primary metric: mean wait, or mean bounded slowdown (which weighs short jobs' waits more) | Mean wait; bounded slowdown reported |
| E-B | Adaptive variants to run | A1, A2 and A3 |
| E-C | The bar: the best single static setting over all workloads, or the best per workload | Best single (`S*`); per-workload best reported. Since 2026-11-27 the static settings are C0 and B1 only (§8.2) |
| E-D | Guards against `S*` | Size-class mean wait +25%, utilization −1 point, Jain −0.05 |
| E-E | Repeat runs for the noise band | Three runs of C0 and A1 on seed 1 of each scenario |

---

## 9. Revised strategy (Phase 4)

*Approved 2026-11-27, requested after the Phase 3 answer was no. Open questions (§9.9): S-B
(wait targets), S-C (a 5,000-point boost) and S-G (median; class gate 10% of the waiting
jobs; levels kept) answered; defaults kept for S-A, S-D, S-E and S-F.*

### 9.1 What changes and why

The user's revision:
1. drop the state that favors small jobs;
2. use B1 (§8.2) as the baseline policy;
3. trigger a change from the current state of the queue;
4. once a favored state's waiting period is over, return to the baseline and hold it for a
   **stabilization period**, set by evaluation;
5. judge whether the targeted jobs wait less while their state is active, as well as the
   regular metrics;
6. set everything up on one-day workloads of different traffic patterns; test later on
   five-day workloads.

| Phase 3 result (§8.6) | What the revision does about it |
|---|---|
| Favoring small never helped small jobs, and small-state periods carried most of the extra waiting. | No small state. |
| Favoring medium cut medium waits by 55–91% on `N` and `Y`, and favoring large helped large jobs on `N`. | Keeps medium and large as the only states to switch to. |
| A backlog built under one state was waited out under the next, so states chained into each other. | No state-to-state switches. Every favored spell ends in the baseline, and the baseline holds for a set time. |
| The Phase 2–3 triggers asked whether the queue's class mix was unusual, against a calibration run. Calibrated under B1, that would count B1's own large-job backlog as normal. A3's arrivals signal was the weakest on `X` and `Y`. | The trigger asks whether a class's waiting jobs have waited past a target (§9.3). It reads the waiting queue only, and needs no calibration run. |

Under B1 there is room for the two states to help. One-week means: large jobs wait 26 h on
`N` (C0: 19.5 h), and medium jobs wait 19 h on `X` (C0: 11 h).

**Question for Phase 4.** Can temporarily favoring medium or large jobs, triggered when that
class's waiting jobs have waited past a target and always followed by a return to B1, shorten
waits compared with B1 alone? Two parts:
- overall, by the regular metrics;
- for the favored class, while its state is active.

### 9.2 Policy states

| State | Configuration | Weights |
|---|---|---|
| `base` | B1 | B1 as published (§8.2) |
| `medium` | B1M: B1, favor medium | B1, plus `PriorityWeightQOS = 5000` with QOS priorities small 0, medium 100, large 0 |
| `large` | B1L: B1, favor large | B1, plus `PriorityWeightQOS = 5000` with QOS priorities small 0, medium 0, large 100 |

- **Static keys:** adaptive runs use B1's static keys for the whole run (§8.2): decay and
  maximum age of 14 days, no `SMALL_RELATIVE_TO_TIME`, partition weight 1000. The hook
  changes only the five weights and the QOS priorities (§7.3), so all three states share
  those keys. The hook and image need no change.
- **Strength (S-C, answered 2026-11-27: in between).** A favored job gets 5,000 extra
  points over every other job:
  - **The bounds:** soft would be about 2,000 points, one typical fair-share gap. Strict
    would be over 12,000, the largest gap B1's other factors can make (fair share 10000 +
    age 1000 + job size 1000), so the favored class always comes first.
  - **What 5,000 means:** under B1, fair share decides most of the order. The typical
    fair-share gap between two waiting jobs is 0.17 (`config/factor_scales.yaml`), about
    1,700 points, while age and job size add tens to hundreds. So 5,000 points is about three
    typical gaps, and half the fair-share weight.
  - **Effect:** a favored job comes first unless the other job's account is far less used
    than its own, by a fair-share factor about 0.5 higher. Fair share keeps a say for the
    most under-served accounts, and B1's order holds within each class.
  - **Large state:** only large is raised. C1 also put medium second (50), but with a
    partial boost that would give medium half a boost during large spells.
  - 5,000 is also near the geometric midpoint of the two bounds (about 4,900).
- **Rendering:** `sim/weights.py` renders `config/priority/B1M.*` and `B1L.*` from a
  `favored_on` block in `config/weights.yaml`, so the weights are never typed by hand.

### 9.3 Trigger and return to baseline

**Signal (S-B, answered 2026-11-27: wait targets).** At each tick, for each candidate class
`c`, the controller takes the class's waiting jobs from the snapshot. It computes `w_c`, the
median of their `waited_s` (time since submit, §7.2), and compares it with a **target** `τ_c`
for that class.

This asks "has this class waited too long?" rather than "is the queue's mix unusual?". So there
is no calibration run and no percentile table (§6.3 is not used). The snapshot already carries
`waited_s`, so the hook does not change.
- **Class gate:** `w_c` counts only when class `c` makes up at least `min_share` (10%) of
  the waiting jobs. The trigger then reacts to a class that is a real part of the queue, not
  to a few leftover jobs.
  - The gate scales with the queue: 40 jobs when 400 are waiting, 4 when 40 are.
  - Over the Phase 2 `K` week under C0, medium passed it at 64% of busy ticks and large at
    45%. B1's queue has not been measured this way; stage 1 reports how often each class
    passes (§9.5).
- **Whole-queue gate:** as in §6.4, nothing triggers when fewer than `n_min` (20) jobs are
  waiting. On a short queue, 10% is one or two jobs, and priority hardly matters there.
- **Candidates:** medium and large. Small is logged (count and median wait) but never
  triggers.
- **Targets (S-G):** three levels are tried (§9.5). Large's target is three times medium's,
  because a large job needs more free room before it can start.

| Level | `τ_medium` | `τ_large` |
|---|---|---|
| tight | 2 h | 6 h |
| middle | 4 h | 12 h |
| loose | 8 h | 24 h |

The levels span the class mean waits B1 gave over the Phase 3 weeks: medium 0.7–19 h, large
2–26 h. Those waits only place the grid; nothing is fitted to them.

**State machine.**

```
            trigger: c ∈ {medium, large} with w_c ≥ τ_c for 2 ticks, c ≥ 10% of the queue
   ┌──────────────┐ ───────────────────────────────────────────▶ ┌──────────────┐
   │ base (armed) │                                               │ favor c      │
   └──────────────┘ ◀──────┐                                      │ for T_active │
                           │ T_stable elapsed                     └──────┬───────┘
                    ┌──────┴────────────┐                                │ T_active elapsed
                    │ base (stabilizing)│ ◀──────────────────────────────┘
                    │ queue not read    │
                    └───────────────────┘
```

| Rule | Value |
|---|---|
| Tick, persistence | 10 min, 2 ticks, as in §6.4 |
| Gate | Class `c` is at least `min_share` (10%) of the waiting jobs, and at least `n_min` (20) jobs are waiting in all |
| Enter | Only from `base (armed)`, when a candidate has `w_c ≥ τ_c` on 2 consecutive ticks. If both classes do, the one with the larger `w_c / τ_c` wins. |
| Favored spell `T_active` | The waiting period, a fixed length: 60 min for both states by default (S-E; one value per state allowed). No early exit, no switch to the other class. |
| Return | At the first tick after `T_active`, the state goes back to B1, whatever `w_c` is then. `P_exit` is not used. |
| Stabilization `T_stable` | B1 holds for this long, and the trigger is not evaluated. The persistence streak starts again from zero when it ends. Set by evaluation (§9.6) |
| Start | `base (armed)` |

- **Cycle:** one favored spell plus its stabilization lasts `T_active + T_stable`. At most
  `24 h / (T_active + T_stable + 20 min)` spells fit in a day, for example about 10 with
  60 + 60 min.
- **Log (§7.5):** each tick logs, per class, the waiting count, `w_c` and `w_c / τ_c`,
  plus `phase` (`armed`, `active` or `stabilizing`) and the time left in it.
- **No stabilization:** with `T_stable = 0`, the policy can re-enter after the 2-tick
  streak. So the `T_stable = 0` runs measure what the stabilization period adds.
- **Replay:** as in Phase 2, the logged snapshots must reproduce every online decision.

### 9.4 One-day setup workloads (`D`)

| Item | Value |
|---|---|
| Name | `D-<pattern>-s<seed>`, seeds 1–3 |
| Patterns | `nat` (natural mix), `flood` (small-short flood), `lglong` (large-long heavy), `acct` (one account submits about half the demand), `mem` (memory-heavy medium), `alt` (6-hour blocks alternating flood and large-long), `over` (`Y`-style: natural background at 60% of `ρ*` plus three bursts of 3–12 h, each adding 40%). Built as in §3.5. |
| Lead-in (S-D) | 12 h of natural mix at `ρ*` before the measured day, so the day starts on a loaded cluster. On `X`, day 0 began on an empty cluster, and its mean wait was 0.0 h under both C0 and B1. Lead-in jobs run and are scheduled, but are not measured. |
| Measured day | 24 h of the pattern. Each job carries `segment` = `leadin` or the pattern name. |
| Load | Thinned to `ρ* = 0.95` in the saturated resource, as in §3.4 |
| Source (S-F) | Setup draws only from trace weeks 1–12 (Jan–Mar; `K` is week 4). The five-day tests (§9.8) draw only from weeks 13–23 (Apr–Jun; `N` is week 20). Nothing is tuned on the months it is tested on. This is like a site tuning on past months and running on later ones. |
| Size | 21 workloads (7 patterns × 3 seeds) |

Each run drains as in §8.4: submissions span 36 h, and the run continues until every job has
ended.

### 9.5 What is compared on the setup workloads

| Id | Kind | Setting |
|---|---|---|
| B1 | static | The baseline, and the bar (S-A) |
| C0 | static | Reference only. It was the best fixed setting in Phase 3, so it is reported, but it is not the bar. |
| A4-`L`-`T` | adaptive | §9.3 with target level `L` (tight, middle, loose) and `T_stable = T` minutes (S-E) |

The runs come in two stages, because the best stabilization period can depend on how often
the trigger fires:

| Stage | Runs | Count |
|---|---|---|
| 1: targets | B1, C0, A4-tight-60, A4-middle-60 and A4-loose-60 on the 21 workloads | 105 |
| | Repeats: B1 and A4-middle-60 on seed 1 of each pattern, two more runs each | 28 |
| | Choose the target level `L*` (§9.6) | |
| 2: stabilization | A4-`L*`-`T` for `T` ∈ {0, 30, 120, 240, 480}; 60 comes from stage 1 | 105 |
| Total | | 238 one-day runs |

Batches, checks and reruns follow §8.4.

### 9.6 Metrics and choice of the stabilization period

**Regular metrics.** Those of §8.5, computed on the measured day's jobs only. Cluster state
is taken over the measured 24 h.

**While a state is active** (`evaluation/favored.py`, extended). Each job's wait is split
across the phases: `active-medium`, `active-large`, `stabilizing`, `armed`. Jobs whose wait
was mostly (at least 50%) in a phase are compared with the same jobs in the B1 run of the same
workload, paired by job name. Three groups:
- **Targeted:** class `c` jobs during `active-c`. This is the question in §9.1.
- **Others:** the other classes during `active-c`, which is the cost.
- **Stabilizing:** all jobs during `stabilizing`. It shows whether the return to B1 settles
  the queue, or the favored spell's backlog lands there.

The net effect of each favored spell is also given, in job-hours saved and lost.

**Adaptation.** Reported for each setting:
- spells per day, by state;
- time in each phase;
- `w_c` at each trigger and at the end of its spell: did the spell bring the class's wait
  down?
- how many spells start at the first armed tick after a stabilization period, which means
  the trigger was still holding.

**Noise band.** Per pattern, the relative range of mean wait over the three runs of B1 and
over the three runs of A4-middle-60 on seed 1, the larger of the two (§8.6).

**Choosing the target level, then `T_stable` (S-F).** The same rule picks `L*` among the
stage 1 settings and `T_stable` among the stage 2 settings:
- For each setting, compute each workload's ratio of mean wait to B1's, and take the
  geometric mean over the 21 workloads. Every pattern then counts the same, whatever its
  wait scale.
- The lowest score wins.
- If several settings score within the noise band of the lowest, take the one with the
  fewest guard failures (§8.6: size class +25%, utilization −1 point, Jain −0.05, all
  against B1). After that, take the looser level, or the longer `T_stable`: fewer spells,
  closer to the baseline.

**Setup result.** Phase 4 reports, for the chosen setting and for all eight A4 settings:
- whether it beats B1 beyond noise, by pattern;
- whether the targeted jobs wait less while their state is active, by pattern and state;
- the guards.

This is a setup result, tuned on these workloads. The test is §9.8.

### 9.7 Outputs

- `config/scenarios.yaml`: a `day` section for `D`; `workloads/build.py` builds it.
- `config/weights.yaml` → `config/priority/B1M.*`, `B1L.*`.
- `config/triggers_A4.yaml`: state map, candidates, class gate, the three target levels,
  `T_active`, `T_stable`. No percentile tables.
- `config/evaluation_setup.yaml`: workloads, policies, repeats, metric and guard settings.
  `config/evaluation.yaml` stays as it is, so Phase 3 can still be reproduced.
- `results/phase4/`: `metrics.csv`, `selection.json` (scores and the choice in each stage),
  `favored.json`, figures; raw runs under `results/phase4/runs/` (gitignored).

### 9.8 Five-day tests (later)

Specified when Phase 4 is done. Outline:
- five-day workloads from trace weeks 13–23 only;
- natural, shifting (one pattern a day) and overlapping-burst kinds, 3 seeds;
- B1, C0 (reference) and A4 at the chosen target level and `T_stable`;
- §8.6's pass rules with B1 as the bar, and the targeted-jobs analysis of §9.6.

### 9.9 Open questions

S-B, S-C and S-G answered 2026-11-27; the defaults for S-A, S-D, S-E and S-F approved the same
day.

| # | Question | Default |
|---|---|---|
| S-A | The bar: B1 (the baseline A4 returns to), or the better of B1 and C0 as in §8.2 | B1. C0 is run and reported as a reference. |
| S-B | Trigger signal from the queue | **Answered 2026-11-27:** the median wait of each class's waiting jobs against a target per class (§9.3) |
| S-C | Strength of the favored states: strict (favored class always first), soft (about one typical fair-share gap, about 2,000 points), or in between | **Answered 2026-11-27:** in between. `PriorityWeightQOS = 5000`, with only the favored class's QOS raised (§9.2) |
| S-D | Lead-in before the measured day | 12 h of natural load, not measured |
| S-E | Favored spell and the stabilization periods tried | `T_active` 60 min for both states; `T_stable` ∈ {0, 30, 60, 120, 240, 480} min |
| S-F | How the target level and `T_stable` are chosen, and the source split | In each stage, the lowest geometric-mean ratio of mean wait to B1 over the 21 setup workloads; ties broken by guards, then the looser level or longer period. Setup from weeks 1–12, tests from weeks 13–23. |
| S-G | Target statistic, class gate and levels | **Answered 2026-11-27:** median wait of the class's waiting jobs. Class gate: a share of the queue, not a count (requested): 10% (confirmed), with the whole-queue gate of 20 kept. The three levels kept (medium / large): 2 / 6 h, 4 / 12 h, 8 / 24 h |
