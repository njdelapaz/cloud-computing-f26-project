# Specification: Adaptive Ordering and Placement Policies for Slurm

**Status:** Approved at the end of phase P0. This expands the original brief (Appendix A) and keeps its goal numbering. Goals 1 and 5 of the brief mention only priority weights; this spec extends them to cover job placement as well (§2). Decisions, and the items later phases will settle, are in §11. Any change goes through the decisions log in `PROGRESS.md` first.

## 0. Scope rule
All work, code, configs and references stay inside `ver_0.2/`. Nothing outside it is read or reused.

---

## 1. Problem
For every pending job, Slurm makes two separate decisions, each fixed by configuration:
- **Order:** which job is considered first. The `priority/multifactor` plugin computes this as a weighted sum of normalized factors:
  ```
  priority = W_age·f_age + W_fs·f_fairshare + W_size·f_jobsize + W_part·f_partition + W_qos·f_qos + Σ W_tres[t]·f_tres[t]
  ```
- **Placement:** which node a starting job lands on. The `select/cons_tres` plugin decides this.

No single fixed configuration is best under every load. Fairness is about who goes first. Balance (even load across nodes) and utilization (no capacity stranded by a bad fit) are about where jobs go.

We will build two cooperating plugins and a controller:
- `priority/adaptive` sets job **order**. Only fairness drives it.
- `select/cons_tres_adaptive` sets job **placement**. Balance and utilization drive it.
- The controller picks the active **policy**, meaning an ordering part plus a placement part. It decides automatically from observed system state, or follows a schedule of admin-defined time windows.

We will then measure whether this beats any single static policy, and how much the switching costs.

## 2. Goals and acceptance criteria

| # | Goal | Done when |
|---|------|-----------|
| G1 | Two plugins forked from the simulator's Slurm 23.11 source. **`priority/adaptive`**, a fork of `priority/multifactor`, takes its ordering weights from the active policy. **`select/cons_tres_adaptive`**, a fork of `select/cons_tres`, ranks candidate nodes by the active policy's placement score (§6.1). Its name has to contain "cons_tres": slurmctld turns on cons_tres-only code by checking `SelectType` for that string | With switching off and `BASE` loaded (stock ordering weights, placement weights 0), both job start order **and** node assignments are **identical** to stock multifactor + cons_tres on the same trace (regression check) |
| G2 | Policy schedule with two modes, **override** (a scheduled window is active) and **auto** (no window, so the controller picks) | On a trace with a window `[t1, t2)`, the decision log shows override from t1 and auto from t2, both within one controller tick |
| G3 | Policy-update mechanism (ordering and placement), overhead measurement, minimum wait time `T_min` | Switch cost, placement cost and disruption are measured (§8), and `T_min` is chosen by a rule written down **before** the sweep, with a plot supporting it |
| G4 | System states that trigger switches, with no sampling while `T_min` blocks a switch | Decision log has zero state evaluations inside cooldown or override. Each trigger workload fires its intended state. The general workload rarely switches |
| G5 | Policy set: single-metric policies plus ordered composites, each with an ordering part (fairness) and a placement part (balance, utilization) | In static runs, each single-metric policy leads the other single-metric policies on its own metric |
| G6 | Workloads from the real trace in `dataset/`: general, trigger patterns, a 5-hour peak window | Sampled traces hit their target offered load, and episode onset times are recorded as ground truth |
| G7 | Evaluate with the UB Slurm simulator | Results table and plots regenerate from one command, with confidence intervals across seeds |

## 3. Non-goals
- No prediction of job resources or runtimes. The scheduler uses only requested values. (Workloads still give jobs actual runtimes at or below the requested walltime, as real traces do.)
- Only the policy's ordering weights and placement weights change at runtime. Backfill parameters, partitions, QOS, `PriorityFlags` and decay settings stay fixed.
- No moving, pausing or preempting running jobs. Placement is decided once, when a job starts.
- Placement scoring covers single-node jobs, which is every job after filtering (§9.2). Any multi-node request falls back to stock node selection.
- No GPUs or other accelerators. The cluster has CPU nodes only, and every job is a CPU job.
- Policy selection is rule-based, with no learned or RL controller.
- Nothing is deployed on a real cluster. Everything runs in the simulator.

---

## 4. Architecture

Everything runs **inside slurmctld**:

```
slurmctld
 ├─ priority/adaptive   (fork of priority/multifactor)
 │    ├─ controller: ticks every PriorityCalcPeriod and picks the active policy
 │    └─ ordering:   priority = Fairshare·f_fairshare + Age·f_age, weights from the active policy
 ├─ shared policy state (written by the controller, read by the placement scorer)
 └─ select/cons_tres_adaptive   (fork of select/cons_tres)
      └─ placement:  when a job starts, or backfill tests where it would run,
                     rank the nodes that fit by the active policy's placement score
```

The controller step:

```
every PriorityCalcPeriod (simulator clock):
  controller_step(now):
    if a schedule window covers now:          mode = OVERRIDE, want = window.policy   (no state sampling)
    elif now - last_switch < T_min:           mode = AUTO, cooldown → skip           (no state sampling — G4)
    else:                                     mode = AUTO, sample state → classify → want = policy_for(state)
    if want != active:
        publish want to the shared policy state
        if its ordering weights differ: recompute priority of all pending jobs (one pass, job write lock)
        placement weights apply from the next node choice; nothing to recompute
        last_switch = now; log the switch
  normal multifactor recalculation continues
```

**Why in slurmctld and not an external daemon:** the UB simulator runs slurmctld on a virtual, accelerated clock. An external process polling `squeue` would run on wall-clock time and race the simulation. Logic inside slurmctld sees the same clock as the scheduler and is deterministic for a given trace.

**Sharing the policy between plugins:** both plugins are loaded into the same slurmctld process. A small shared module compiled into slurmctld holds the active policy; the controller writes it and the scorer reads it. The exact hook is settled while building the plugins (A2, A4).

**Backfill consistency:** the scorer is used both when a job actually starts and when backfill tests where a job would run. That way backfill's plans and the real starts agree on nodes.

**Testability:** the decision logic is a pure function `decide(snapshot, config, now, controller_state) → decision`, and the node scorer is a pure function `score(job, node_state, policy) → number`. Neither uses Slurm locks or globals, so both can be unit-tested outside slurmctld.

### 4.1 Config files (`ver_0.2/config/`)
The plugins read these at startup and on `scontrol reconfigure`. The exact grammar and rules are in §13.1.
- **`policies.conf`:** one line per policy, with its ordering weights (`Fairshare`, `Age`, `JobSize`) and placement weights (`Spread`, `Fit`). `scripts/make_policies.py` generates it from the §6 mapping.
- **`schedule.conf`:** override windows, one-off or daily.
- **`adaptive.conf`:** controller settings. These are file paths, `TMin`, `DefaultPolicy`, signal thresholds, log paths, and two experiment modes (`FixedPolicy` for static runs, `ForceAlternate` for P5).

### 4.2 Logs
- **Decision log:** CSV, one row per controller tick.
- **Placement log:** CSV, one row per job start, optional.
- Columns and rules for both are in §13.3.
- When a switch changes the ordering weights, the plugin can also dump the pending queue's priority order just before and after it, for churn analysis (§8).

---

## 5. Background constraint: keep each lever on one scale
- **Ordering:** factors are normalized to `[0,1]`, but weights are raw integers. If policies had different weight totals, a switch would also rescale every priority, which skews comparisons and churn numbers. **Every policy's ordering weights therefore sum to 100,000.**
- **Placement:** `Spread + Fit = 1`, or both are 0, which means stock node selection.
- Partition, QOS, JobSize and TRES weights are 0 in every policy except `BASE`. The setup has a single partition and a single QOS.

## 6. Policies (G5)

Three metrics, on two levers:

| Metric | Lever | What it aims for |
|---|---|---|
| **F, fairness** | order | equity across **users/accounts** relative to their shares |
| **B, balance** | placement | even **load across nodes** |
| **U, utilization** | placement | usable capacity: no cores stranded because their node's memory is gone, and the reverse |

A policy is a mix of the three with coefficients `(c_F, c_B, c_U)` that sum to 1:

- **Ordering:** `Fairshare = c_F × 90,000` and `Age = 100,000 − Fairshare`. Every policy keeps at least 10,000 on Age, so no job waits forever. The lower fairness ranks, the closer ordering gets to first-come-first-served.
- **Placement:** `Spread = c_B ÷ (c_B + c_U)` and `Fit = c_U ÷ (c_B + c_U)`. If `c_B + c_U = 0` (policy `F`), placement is stock.

| Policy | c_F / c_B / c_U | Fairshare | Age | Spread | Fit |
|---|---|---|---|---|---|
| `F` | 1 / 0 / 0 | 90k | 10k | stock | stock |
| `B` | 0 / 1 / 0 | 0 | 100k | 1.00 | 0 |
| `U` | 0 / 0 / 1 | 0 | 100k | 0 | 1.00 |
| `F>B>U` | 0.6 / 0.3 / 0.1 | 54k | 46k | 0.75 | 0.25 |
| `F>U>B` | 0.6 / 0.1 / 0.3 | 54k | 46k | 0.25 | 0.75 |
| `B>F>U` | 0.3 / 0.6 / 0.1 | 27k | 73k | 0.86 | 0.14 |
| `B>U>F` | 0.1 / 0.6 / 0.3 | 9k | 91k | 0.67 | 0.33 |
| `U>F>B` | 0.3 / 0.1 / 0.6 | 27k | 73k | 0.14 | 0.86 |
| `U>B>F` | 0.1 / 0.3 / 0.6 | 9k | 91k | 0.33 | 0.67 |
| `EQ` | ⅓ each | 30k | 70k | 0.50 | 0.50 |
| `BASE` | — | stock-style ordering weights | | stock | stock |

That makes 10 policies the controller switches to on a trigger, plus `BASE` as the resting state. In auto mode `BASE` runs whenever no signal is active, so auto equals the baseline until a trigger fires. The 0.6 / 0.3 / 0.1 split, the 90k Fairshare scale and the 10k Age floor are starting points, tuned in P4.

### 6.1 Placement scoring (new in `select/cons_tres_adaptive`)
When a single-node job fits on several nodes, each candidate node *n* gets a score:

```
job share:     j = (job cores ÷ node cores, job memory ÷ node memory)
free before:   f = (free cores ÷ node cores, free memory ÷ node memory)
free after:    f' = f − j

spread(n) = min(f'_cores, f'_mem)       headroom left on the node's tighter resource
fit(n)    = cos(j, f)                   how well the job's mix matches what the node has left
score(n)  = Spread·spread(n) + Fit·fit(n)
```

The job goes to the highest-scoring node, and ties fall back to Slurm's stock order. With both weights at 0, stock selection runs unchanged.

- **Spread** sends each job to the emptiest node, which evens out load across nodes.
- **Fit** sends each job to the node whose leftover cores-to-memory mix matches the job's own. Core-heavy jobs use cores stranded next to full memory, and memory-heavy jobs use memory stranded next to busy cores. This is the same idea as the alignment score used in multi-resource packing schedulers such as Tetris.
- **Example:** a job asks for 8 cores + 8 GB. Node A is empty. Node B has 8 cores and 96 GB in use, so 24 cores are free with only 32 GB to go with them.
  - Spread prefers A (0.75 vs 0.19).
  - Fit prefers B (1.00 vs 0.86), which puts B's stranded cores to work.
  - Policy `U>F>B` (Spread 0.14, Fit 0.86) scores A 0.84 and B 0.88, so it picks B.
  - Policy `F>B>U` (Spread 0.75, Fit 0.25) scores A 0.78 and B 0.39, so it picks A.
- **Cost:** scoring 16 nodes is cheap, but it runs on every start and every backfill test, so its cost is measured (§8).

### 6.2 What the simulator can and can't show
- **No speedups:** the simulator replays each job's recorded runtime, so jobs don't run faster on emptier nodes. Balance's benefit shows in its own metric (how even node load is) and in side effects on waits. Any real-world speedup from less crowded nodes is outside what this setup can measure.
- **B and U pull against each other:** spreading leaves fewer whole nodes free, which can delay whole-node jobs, while fit tends to pack nodes. The composite policies explore that trade-off.

## 7. System states (G4)

All signals come from one in-memory snapshot of pending and running jobs, node state and association usage. Each signal is in `[0,1]`. A node's **load** is the larger of its allocated core fraction and its allocated memory fraction.

| Signal | Meaning | Formula |
|---|---|---|
| `queue_pressure` (gate) | Is there contention at all? | pending requested core-hours ÷ (cluster cores × 1 h). Below `QueuePressureMin`, fairness and utilization don't count |
| `s_fairness` | One user or account dominates the waiting work | 1 − Jain index of per-user pending demand ÷ share, over users with pending jobs |
| `s_balance` | Node loads are uneven | standard deviation of node load ÷ 0.5. It measures how running work is spread, so the queue gate doesn't apply. It counts only when at least `MinDemand` of the cluster's cores are busy |
| `s_util` | Capacity is stranded while work waits | the share of free cores that are stranded, meaning their node's free memory per free core is below the median memory per core of pending jobs. Counts only when pending demand is at least `MinDemand` of capacity |

**Classification (maps states onto the policy set):**
- Hysteresis: a signal becomes *active* when it crosses `Enter` and stays active until it falls below `Exit`.
- No active signal: `DefaultPolicy` (`BASE`).
- One active signal: that signal's single-metric policy (`F`, `B` or `U`).
- Two or more active: the ordered composite ranked by normalized severity `s / Enter`. For example, `s_F/θ_F > s_U/θ_U > s_B/θ_B` gives `F>U>B`.

**Sampling rule:** no snapshot is taken during cooldown or override, which keeps the controller cost at zero when it can't act anyway.

**Calibration:** `Enter` is set at the p90 and `Exit` at the p75 of each signal's distribution in static `BASE` runs of the general workload, using **calibration seeds only**. Evaluation uses held-out seeds.

## 8. Policy updates, overhead, and T_min (G3)

**Mechanism:** publish the new policy to the shared state. If its ordering weights differ from the old policy's, recompute every pending job's priority in a single pass under the job write lock. New placement weights take effect at the next node choice, with nothing to recompute. Running jobs are never moved.

**Overhead, measured at five levels:**
1. **Controller cost:** µs per switch and µs per state sample, against the number of pending jobs (scaling plot).
2. **Placement cost:** µs per node choice with scoring vs stock selection. Scoring runs on every start and every backfill test.
3. **Scheduler impact:** main and backfill cycle times (sdiag counters), overall and just before and after each switch.
4. **Disruption from ordering changes**, measured only when the ordering weights change:
   - queue-order churn: Kendall-τ distance between pending order just before and after a switch
   - reservation churn: pending jobs whose expected start moved later
   - bumped jobs: jobs that were next in line but lost their place

   A placement change doesn't move anything already running, so its disruption shows up only in later waits and is caught by level 5.
5. **End-to-end cost of switching:** forced alternation between two very different policies (`F` and `U`, which differ on both levers) every *P* ∈ {1 tick, 15 m, 30 m, 1 h, 2 h, 4 h}, compared with each one run statically. This isolates what switching costs from whether the decisions were any good.

**Timing:** the simulator wraps `time()`, `gettimeofday()` and the sleep calls to return simulated time, but not `clock_gettime()`. So `swap_us` and `score_us` are measured with `CLOCK_MONOTONIC`, which stays real (confirmed in P1). Backfill cycles also advance the simulated clock by their real duration, so the scheduler's real CPU cost, the scorer's included, shows up in simulated time.

**T_min selection:**
- Sweep `T_min` ∈ {1 tick, 15 m, 30 m, 1 h, 2 h, 4 h, ∞} in auto mode, on the trigger workloads plus the general workload.
- **Adaptivity:** response latency (trigger onset to switch) and metric gain over `BASE`.
- **Disruption:** switches per simulated day, total churn, and the change in p95 wait.
- **Rule (fixed before running):** take the smallest `T_min` whose p95 wait on W0 stays within **X%** of the `T_min=∞` (never switch) case, provided trigger workloads W1–W3 still get a switch within **Y** of onset. X and Y are set once P5's forced-switch results are in, and written into this spec before the sweep starts.
- `T_min` is a whole multiple of `PriorityCalcPeriod`. `PriorityCalcPeriod` stays the same across **all** runs, baseline included.

**Override vs cooldown:** an override window's start and end take effect immediately, regardless of cooldown, because admin intent wins. The end of a window starts a fresh cooldown so auto mode doesn't flip right away.

**Stretch goal, after the main evaluation:** compare hot swap against switching by `scontrol reconfigure` with a swapped config file, on the same overhead measures.

## 9. Workloads (G6)

**Cluster model:** kept small so the simulator runs fast.
- 16 CPU nodes × 32 cores (512 cores), 128 GB memory each (`RealMemory=128000` MB, 2 TB total). No GPU nodes.
- One partition and one QOS. The partition's `MaxTime` is 7 days, the longest time limit in the kept trace.
- Slurm tracks memory as a consumable resource (`SelectTypeParameters=CR_Core_Memory`), and every job requests memory with `--mem`, so no job falls back to a default that takes a whole node's memory.
- Users and accounts come from the trace (about 649 users in 94 accounts after filtering). Every account gets an equal fair-share share.

All workloads come from the real job trace in `dataset/`. There is no synthetic generator.

### 9.1 Source trace (`dataset/`)
- `01.tsv` to `06.tsv`: one file per month of job **end** times, January–June 2024, with months split at Paris local midnight. 23.5M jobs in total, no duplicate job IDs. The `.tar.gz` files are compressed copies of the same six files.
- One row per job: `jobid, state, account, user, partition, submit, eligible, start, end, timelimit, elapsed, allocmem, maxrss, alloccpus, totalcpu, allocgpus, started, maxdiskwrite, nsteps, el9, hostnames`. Times are Unix seconds, `timelimit` and `elapsed` are in seconds, and `allocmem` is in MB.
- The source cluster averaged about 13,100 busy cores on a few hundred CPU nodes, about 25× the simulated cluster. Monthly averages ran from 11,900 to 13,800; the daily median was 13,600, with a range of 3,800–17,500.

### 9.2 Converting the trace

**Field mapping:**

| Trace field | Simulated job | Note |
|---|---|---|
| `submit` | submit time | `eligible` equals `submit` for 93% of jobs, so job dependencies aren't modeled |
| `alloccpus` | cores requested, on one node | |
| `allocmem` | `--mem` (MB) | |
| `timelimit` | `--time`, the requested walltime | |
| `elapsed` | actual runtime the simulator replays | capped at `timelimit`. 1.66% of kept jobs ran past their limit, by a median of 16 s (the kill grace period), and almost all are `timeout` jobs. Capped, they time out at the limit as they did for real |
| `user`, `account` | user and account for fair-share | |
| `partition` | not kept | every job goes to the single partition |
| `state` | used only for filtering | failed and timed-out jobs still held resources, so they stay |
| `maxrss`, `totalcpu`, `maxdiskwrite`, `nsteps`, `el9` | not used | actual usage is out of scope (§3) |
| `hostnames` | used only to drop multi-node jobs | |

**Filters, in order:**
1. Keep partitions `htc`, `htc_highmem` and `hpc`. This drops `gpu` and `gpu_interactive` (no GPUs in this project), `htc_daemon` (service jobs with limits up to 90 days), and `htc_interactive` and `flash` (interactive and very small). About 54k jobs go.
2. Drop jobs that never started (about 289k, counting `started = 0` as never started; mostly cancelled while pending) and jobs with zero elapsed time (about 328k).
3. Drop jobs that don't fit one node: more than 32 cores, more than 128 GB, or multi-node. That's 0.05% of jobs and 1.1% of core-hours.

About 22.9M jobs remain, all single-node.

**Scaling by random job sampling:** each seed draws an independent random sample of the filtered trace, keeping each job with probability *p*. The sample's offered load (Σ cores × runtime of jobs submitted in the measured window ÷ (512 × window length)) targets 0.85.
- **How p is set:** a fixed *p* per window misses the target too often, because a few long, wide jobs dominate a day's core-hours. Instead each job gets a seeded random key u ~ U(0, 1).
  - The measured window's jobs are taken in increasing u until the load reaches 0.85.
  - *p* is the key of the job that gets there; it is recorded as `sampling_rate` in `meta.json`.
  - A job that would push the load more than 2% over is skipped.
  - Warm-up jobs are kept when u ≤ *p*.
- **Result:** the load lands in [0.85, 0.867] (all 640 samples in B1's sweep were within ±5%). *p* averages about 3.3% and ranged from 1.1% to 3.9% in B1's gate windows.
- **Cost:** the very largest jobs, each worth over 20% of a day's target, are kept at about half the nominal rate. Jobs worth up to 5% are kept at 0.99–1.04× the nominal rate.
- **What sampling keeps:** the real mix of job shapes, users and daily cycle. Bursts shrink in absolute size but keep their size relative to the cluster.

**What the trace says about these jobs** (these replace the earlier generator assumptions):
- **Sizes:** median 1 core, p90 2 cores, p99 12 cores.
- **Memory per core:** 61% of jobs ask for up to 4 GB per core (the node's own ratio), 30% for 4–8 GB, and 9% for more than 8 GB. Memory-heavy jobs are common, so fit-based placement has real work to do.
- **Requested vs actual time:** for completed jobs, the time limit is a median 11× the runtime, with a long tail (p75 above 300×).
- **User skew:** the top user accounts for 18% of core-hours and the top 10 users for 51%. The top account accounts for 20%.
- **Daily cycle:** the busiest 5 hours are 12:00–17:00 UTC, with 33% of submitted core-hours (an even spread would give 21%), about 1.6× the average rate. The day has a morning and an afternoon peak, so the 5-hour peak isn't sharp.

### 9.3 Workloads from real episodes
The trigger workloads are windows of the trace where the pattern happens on its own. Nothing is injected.

| ID | Workload | Purpose | How it's found (starting thresholds, reviewed in B1) | Found in the full trace |
|---|---|---|---|---|
| W0 | General | baseline, false-trigger check | windows with none of the episodes below | — |
| W1 | User flood | fires **fairness** | a 6-hour window where one user submits over half the core-hours and the total is above 1.5× the median | 58 windows from 22 users, merging into 55 episodes |
| W2 | Memory-heavy burst: memory-heavy jobs fill nodes' memory and strand their cores | fires **utilization** | a 6-hour window where jobs above 8 GB per core make up more than 3× their median share of submitted core-hours, **and** their core-hours are at least their median per window (a volume floor added in B1's review, which drops near-idle windows) | 84 episodes |
| W3 | Uneven node load: load piles up on some nodes while others sit nearly empty | fires **balance** | needs `BASE` simulation runs; found in P6 | not yet known |
| W4 | Sequence | several correct switches in one run | a window holding episodes of at least two kinds back to back | 19 chains of W1 and W2 episodes within one day (W3 joins once P6 finds it) |
| W5 | Oscillating: signals flip faster than a plausible T_min | tests cooldown and hysteresis | needs `BASE` simulation runs; found in P6 | not yet known; may not exist |
| W6 | Daily peak | override vs auto | the natural peak, 12:00–17:00 UTC, over several days | every day in the profile (about 1.6× the average rate, not the ×2.5 first assumed). Day by day the median peak ratio is 1.43, and 56 of 182 days, mostly weekends, have no peak, so W6 runs pick strong weekdays |

**Onset times:** each episode's estimated start is recorded in a sidecar file and used as ground truth for response latency.
- **W1 and W2:** the start of the burst's submissions, refined to the hour.
- **W3 and W5:** the first time the signal crosses `Enter` in the `BASE` run.

**Calibration vs evaluation:** different seeds of the same window contain the same real events, just a different sample of jobs, so tuning and testing on one window would share those events. Calibration therefore uses windows from January–February, and evaluation uses windows from March–June. Both sets contain episodes; for example, the user floods split 21 to 37.

**Run length:** how long each window is depends on how fast the simulator runs. That gets set once P1 measures it.

**Warm-up:** the first stretch of each trace fills fair-share usage history and is excluded from metrics. Metrics cover only jobs submitted inside the measured window.

## 10. Evaluation (G7)

**Metrics**, computed from job completion records plus the decision and placement logs:
- **Fairness:** Jain index over per-user mean bounded slowdown; worst-user / best-user mean wait.
- **Balance:** time-averaged standard deviation of node load; time-averaged maximum node load.
- **Utilization:** core utilization over the measured window (memory utilization reported alongside); stranded core-hours (free cores whose node's memory is too full to use them while jobs wait); makespan.
- **General:** mean, median, p95 and max wait; mean bounded slowdown (τ = 10 s); throughput.
- **Overhead:** switch count, `swap_us`, `score_us` per node choice, churn.

**Run matrix** (each workload × seeds):

| Config | Purpose |
|---|---|
| Static × 11 policies | reference points and the G5 check |
| Forced switch × periods | mechanism cost |
| Auto × T_min sweep | choose T_min |
| Auto at chosen T_min | **main result** |
| Override only (W6 window → `U`, `BASE` otherwise) | does scheduling alone help? |
| Auto + override | combined |

**Headline comparisons:**
1. Auto vs `BASE`, per workload.
2. Auto vs the **best static policy in hindsight**, per workload. Adaptive is worth it if it stays close to the best static policy everywhere while no single static policy wins everywhere.
3. Override vs auto on W6: does knowing the peak window beat detecting it?
4. The switching-cost curve and the T_min justification.

**Seeds:** a seed is one random job sample of a trace window (§9.2). Each workload gets 3 calibration seeds, taken from January–February windows and used for P4 tuning and P6 thresholds. It also gets 5 evaluation seeds, taken from March–June windows and used only for reported results.

Report every metric as mean ± 95% CI across the 5 evaluation seeds. All plots come from one script reading raw results.

---

## 11. Decisions and remaining open items

**Decided:**

| # | Question | Decision |
|---|---|---|
| 1 | Simulator | UB simulator v3.0: `slurm_simulator` @ `f99a71e` and `slurm_sim_tools` @ `21ba58d`. The code is Slurm 23.11.0-0rc1 (per its `META` file), even though the branch is named `slurm-23-02-sim` |
| 2 | What "balance" means | Even load across nodes, achieved through placement (§6.1) |
| 3 | Override vs T_min | Override wins; a window's end starts a fresh cooldown |
| 4 | Device utilization | CPU cores only; no GPUs in the project |
| 5 | Cluster and users | 16 CPU nodes × 32 cores with 128 GB memory each, one partition, CPU jobs only. Users and accounts come from the trace, with equal shares per account |
| 6 | Default policy in auto | `BASE` |
| 7 | Seeds | 3 calibration + 5 evaluation per workload |
| 8 | T_min rule numbers (X, Y) | Set after P5's forced-switch results, before the T_min sweep |
| 9 | `scontrol reconfigure` comparison | Stretch goal, after the main evaluation |
| 10 | Workload source | The real trace in `dataset/`, filtered to jobs that fit one node (§9.2) |
| 11 | Scaling the trace | Random job sampling to about 0.85 offered load; each seed is one sample |
| 12 | Trigger workloads | Real episodes found in the trace, with no injection (§9.3) |
| 13 | Non-goals (§3) | Only ordering and placement weights change at runtime; rule-based selection; simulator only |
| 14 | Weight totals (§5) | Ordering weights sum to 100,000; placement weights sum to 1, or both 0 for stock placement |
| 15 | Calibration vs evaluation | January–February for calibration, March–June for evaluation |
| 16 | Episode-finding thresholds (§9.3) | Starting points; the episodes found get reviewed in B1 and the thresholds adjusted if they don't look like real floods or bursts |
| 17 | What each policy controls | Fairness sets job order. Balance and utilization set node placement (§6) |
| 18 | Placement mechanism | Custom node scoring in `select/cons_tres_adaptive`, a fork of `select/cons_tres` (§6.1). The name keeps "cons_tres" because slurmctld turns on cons_tres-only code by checking for that string (found in P1) |
| 19 | Ordering in composites | Fairshare scales with fairness's coefficient; Age takes the rest, with a 10k floor (§6) |
| 20 | Memory tracking | Confirmed in P1: with `CR_Core_Memory`, the simulator enforces memory per node |
| 21 | Interfaces | Config grammars, data and log formats, controller edge cases and metric definitions are in §13 (P2) |

**Settled by later phases:**
- How the two plugins share the active policy inside slurmctld: settled in A2 and A4.
- `MinDemand` for the balance and utilization signals: calibrated in P6 along with the thresholds.
- W3 and W5 can only be found from `BASE` simulation runs (P6). If either turns out not to exist in the trace, what to do about it needs a decision then.
- Window length per run: P1 measured about 11 minutes of real time per simulated day of submissions on the project cluster (about 530×). The length gets set along with the runner and run matrix (B3, P4).
- X and Y for the T_min rule: set after P5.

## 12. Repository layout (all inside `ver_0.2/`)
```
SPEC.md  WORKFLOW.md  PROGRESS.md
docker/                      simulator image with priority/adaptive and select/cons_tres_adaptive compiled in
plugin/priority_adaptive/    C source + build glue (ordering + controller)
plugin/select_cons_tres_adaptive/  C source + build glue (placement scorer)
plugin/tests/                unit tests for decide() and score()
scripts/                     run, check, generate and validate scripts
tests/                       Python unit tests (validators, later metrics)
examples/                    small example of every data and log format (§13)
config/                      slurm.conf, policies.conf, schedule.conf, adaptive.conf
dataset/                     source trace (read-only, too large to commit)
workloads/convert/           trace filter, random-job sampler, simulator converter
workloads/episodes/          episode finder + onset sidecars
workloads/traces/            sampled, converted traces
experiments/                 run-matrix definitions + runner
analysis/                    metrics + plots
results/                     raw per-run output + summary tables
```

## 13. Interfaces (P2)
Every format below has a validator in `scripts/validate.py` and an example file in `config/` or `examples/`. `scripts/check_p2.sh` checks them all and runs the unit tests in `tests/`.

### 13.1 Config files
All three use one entry per line. `#` starts a comment, blank lines are ignored, and `Key=Value` tokens are separated by spaces.

**`policies.conf`** must define all 11 policies of §6:

| Key | Type | Rule |
|---|---|---|
| `PolicyName` | name | letters, digits, `_`, `-` and `>`; unique |
| `Fairshare`, `Age`, `JobSize` | integer ≥ 0 | sum to 100,000. `JobSize` is 0 except in `BASE` |
| `Spread`, `Fit` | number in [0, 1] | sum to 1, or both 0 for stock placement. `BASE` always uses stock placement |

**`schedule.conf`** (times are UTC on the simulator clock; an empty file means no overrides):
- `Window Start=YYYY-MM-DDTHH:MM:SS End=YYYY-MM-DDTHH:MM:SS Policy=<name>` is a one-off window.
- `Window Daily=HH:MM-HH:MM Policy=<name>` repeats every day and can't cross midnight.
- Each window must end after it starts and name a known policy. No two windows may overlap, whether daily or one-off.

**`adaptive.conf`:**

| Key | Required | Rule |
|---|---|---|
| `PoliciesFile`, `ScheduleFile` | yes | paths |
| `TMin` | yes | a duration such as `90s`, `15min` or `2h`; a whole multiple of `PriorityCalcPeriod` |
| `DefaultPolicy` | yes | a known policy |
| `Signal=<name> Enter=<x> Exit=<y>` | one line each for `fairness`, `balance` and `util` | 0 ≤ Exit < Enter ≤ 1 |
| `MinDemand` | yes | in [0, 1] |
| `QueuePressureMin` | yes | ≥ 0 |
| `LogFile` | yes | where the decision log goes |
| `PlacementLog` | no | where the placement log goes; no placement log when absent |
| `FixedPolicy` | no | pins one policy for the whole run (static runs, mode `FIXED`) |
| `ForceAlternate=A,B` with `ForcePeriod` | no | switches between two policies every period (P5, mode `FORCED`) |

`FixedPolicy` and `ForceAlternate` can't both be set.

**Controller rules these formats encode (decided in P2):**
- At the start of a run no switch has happened yet, so the controller is eligible on its first tick.
- When an override window starts, its policy takes over at once, even during a cooldown. When it ends, the controller switches back to `DefaultPolicy` at once and starts a fresh cooldown.
- In `OVERRIDE`, `FIXED` and `FORCED` mode the controller never samples state.

### 13.2 Data files (written in B1)
**`jobs.csv`** is the neutral trace: one row per job, sorted by submit time.

| Column | Type | Rule |
|---|---|---|
| `job_id` | integer | 1, 2, 3, … in file order |
| `submit` | integer | Unix seconds (UTC), inside the window |
| `user`, `account` | text | from the trace |
| `cores` | integer | 1–32 |
| `mem_mb` | integer | 1–128,000 |
| `time_limit_s` | integer | ≥ 60 and in whole minutes; the simulator takes minutes, so the converter rounds up |
| `runtime_s` | integer | from 1 up to `time_limit_s` (capped, §9.2) |
| `src_jobid`, `src_partition`, `src_state` | text | where the job came from in the trace; the partition is `htc`, `htc_highmem` or `hpc` |

**`meta.json`**, one per sampled window, holds:
- `window_start` and `window_end`, the submit window in Unix seconds
- `warmup_end`: jobs submitted before it are excluded from metrics. The warm-up comes **before** the measured window: `window_start` = start − warm-up, `warmup_end` = start, `window_end` = start + length
- `seed`, `sampling_rate` in (0, 1], and `target_load` in (0, 1]
- the counts `jobs`, `users` and `accounts`, which must match `jobs.csv`

**`episodes.csv`** has these columns:
- `episode_id`, a unique integer
- `workload`, W1–W6
- `signal`: `fairness`, `balance`, `util` or `none`
- `onset` and `end` in Unix seconds, with `end` after `onset`
- `detail`, free text such as `user=user0023`. It always starts with `split=calibration` or `split=evaluation` (by onset), and episodes that reach past 2024-06-23T22:00Z add `coverage=partial`. Jobs submitted after that point and still running at the end of June aren't in the trace

### 13.3 Logs (written by the plugins)
**Decision log**, one row per controller tick:

| Column | Rule |
|---|---|
| `sim_time` | Unix seconds, never decreasing |
| `mode` | `AUTO`, `OVERRIDE`, `FIXED` or `FORCED` |
| `eligible` | 1 only in `AUTO`, once the cooldown has passed |
| `sampled` | 1 only when `eligible` is 1 (G4) |
| `s_fairness`, `s_balance`, `s_util` | in [0, 1] when sampled, empty otherwise |
| `queue_pressure` | ≥ 0 when sampled, empty otherwise |
| `node_load_sd` | in [0, 0.5] when sampled, empty otherwise |
| `active_policy` | the policy in force before the tick, which is the previous row's `chosen_policy` |
| `chosen_policy` | the policy in force after the tick |
| `switched` | 1 exactly when `chosen_policy` differs from `active_policy`. In `AUTO` a switch needs sampling, except on the tick right after a window ends |
| `swap_us` | real microseconds (`CLOCK_MONOTONIC`) spent switching; empty when nothing switched |
| `pending_jobs` | integer ≥ 0 |

**Placement log**, one row per job start:
- `sim_time`, `job_id` (each job appears once), `policy` and `chosen_node`
- `score`: in [0, 1], or empty when the policy uses stock placement
- `candidates`: the number of nodes the job fit on, at least 1
- `score_us`: real microseconds spent scoring, 0 for stock placement

### 13.4 Metric definitions (B2 implements them)
Job metrics cover the **measured jobs**, those submitted between `warmup_end` and `window_end`. Time-averaged metrics integrate over [`warmup_end`, `window_end`).

| Metric | Definition |
|---|---|
| wait | start − submit, in seconds |
| bounded slowdown | max(1, (wait + runtime) ÷ max(runtime, 10 s)) |
| **Fairness:** Jain index | (Σ xᵤ)² ÷ (n · Σ xᵤ²), where xᵤ is user u's mean bounded slowdown, over the n users with measured jobs. 1 means perfectly even |
| **Fairness:** wait spread | the worst user's mean wait ÷ the best user's mean wait |
| node load Lₙ(t) | max(allocated cores ÷ 32, allocated memory ÷ 128 GB) on node n |
| **Balance:** load SD | time average of the standard deviation of Lₙ(t) across the 16 nodes |
| **Balance:** max load | time average of maxₙ Lₙ(t) |
| **Utilization:** cores | ∫ allocated cores dt ÷ (512 × window length). Memory utilization is the same with 2 TB |
| **Utilization:** stranded core-hours | ∫ free cores on nodes whose free memory per free core is below the median memory per core of the jobs pending at that moment, counted only while jobs are pending |
| makespan | last end − first submit, over measured jobs |
| throughput | finished measured jobs per day of makespan |
| general | mean, median, p95 and max wait; mean bounded slowdown |
| overhead | switch count; mean, p95 and max of `swap_us` and `score_us`; churn |
| churn | Kendall-τ distance between the pending order just before and just after a switch: the share of job pairs whose order flipped |
| confidence interval | mean ± t₀.₉₇₅,₄ × s ÷ √5 over the 5 evaluation seeds (t = 2.776) |

---

## Appendix A. Original brief
The brief this spec grew from, as first written.

### 1. Problem
Create a scheduler in Slurm that can change policies based on what it wants to maximize

### 2. Goals
1) Update Slurm using the job priority plugin so that it can update pre-defined priority weight policies based on the state of the system
2) Create a mechanism that can schedule a specific policy for a specific time window. If nothing is scheduled, the system falls back to picking its own policy based on system state
    - Two modes: auto, override
3) Create the weight update mechanism
    - measure the overhead of changing weights
    - determine how often a system can change weights, allowing it to respond to system changes but causing minimal disruption to performance. Create a minimum wait time to change policies that balances system adaptibility and minimal system disruption.
4) Define system states that cause the weights to be updated
    - do not check system state for if the system is unable to change weights because of the minimum wait time
5) Create the weight combinations that act as policies
    - a set of policies that favor single metrics: fairness, balance, device utilization
    - a set of policies that combine these metrics, weighing some over others: an example: fairness > balance > utilization
6) Create simulated workloads
    - general traffic
    - specific traffic patterns that would trigger the weight changes
    - traffic patterns that are heavier during a specific 5 hour window
7) Run simulated workloads using the University of Buffalo's Slurm simulator to evaluate different policies performance

### 3. Non-Goals
- Do not try to predict job resources for better utilization. Only use what the requests have.
