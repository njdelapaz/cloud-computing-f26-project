# Specification: Adaptive Priority-Weight Scheduling for Slurm

**Status:** Proposed. This expands `SPEC.md` (goal numbering kept) and adds design and acceptance criteria. Text marked *(proposed)* goes beyond what `SPEC.md` says and still needs sign-off. Decisions made so far, and the few items still open, are in §11.

## 0. Scope rule
All work, code, configs and references stay inside `ver_0.2/`. Nothing outside it is read or reused.

---

## 1. Problem
Slurm's `priority/multifactor` plugin computes each pending job's priority as a weighted sum of normalized factors:

```
priority = W_age·f_age + W_fs·f_fairshare + W_size·f_jobsize + W_part·f_partition + W_qos·f_qos + Σ W_tres[t]·f_tres[t]
```

The weights `W` are fixed in `slurm.conf`, but no single weight set is best under every load. Weights tuned for fairness can leave nodes idle while large jobs wait, and weights tuned for utilization can starve light users when one user floods the queue.

We will build a priority plugin that switches between predefined weight sets (**policies**) while it runs. It also adds one new factor, `f_balance`, which favors jobs that use whichever resource type is currently idle (§6.1). The switch happens automatically from observed system state, or on a schedule of admin-defined time windows. We will then measure whether this beats any single static policy, and how much the switching costs.

## 2. Goals and acceptance criteria

| # | Goal | Done when |
|---|------|-----------|
| G1 | `priority/adaptive` plugin: a fork of `priority/multifactor` from the simulator's Slurm 23.x source, whose weights come from the active policy, plus a new balance factor (§6.1) | With switching off and the baseline policy loaded (balance weight 0), job start order is **identical** to stock multifactor on the same trace (regression check) |
| G2 | Policy schedule with two modes, **override** (a scheduled window is active) and **auto** (no window, so the controller picks) | On a trace with a window `[t1, t2)`, the decision log shows override from t1 and auto from t2, both within one controller tick |
| G3 | Weight-update mechanism, overhead measurement, minimum wait time `T_min` | Swap cost and disruption are measured (§8), and `T_min` is chosen by a rule written down **before** the sweep, with a plot supporting it |
| G4 | System states that trigger switches, with no sampling while `T_min` blocks a switch | Decision log has zero state evaluations inside cooldown or override. Each trigger workload fires its intended state. The general workload rarely switches |
| G5 | Policy set: single-metric policies plus ordered composites | In static runs, each single-metric policy leads the other single-metric policies on its own metric |
| G6 | Workloads from the real trace in `dataset/`: general, trigger patterns, a 5-hour peak window | Sampled traces hit their target offered load, and episode onset times are recorded as ground truth |
| G7 | Evaluate with the UB Slurm simulator | Results table and plots regenerate from one command, with confidence intervals across seeds |

## 3. Non-goals
- No prediction of job resources or runtimes. The scheduler uses only requested values. (Workloads still give jobs actual runtimes at or below the requested walltime, as real traces do.)
- *(proposed)* Only priority weights change at runtime. Backfill parameters, the select plugin, partitions, QOS, `PriorityFlags` and decay settings stay fixed.
- No GPUs or other accelerators. The cluster has CPU nodes only, and every job is a CPU job.
- Job placement doesn't change. Slurm's node selection decides which nodes a job lands on; policies only change the order jobs start in.
- *(proposed)* Policy selection is rule-based, with no learned or RL controller.
- *(proposed)* Nothing is deployed on a real cluster. Everything runs in the simulator.

---

## 4. Architecture

The controller runs **inside slurmctld** as part of `priority/adaptive` and ticks on the plugin's periodic recalculation (`PriorityCalcPeriod`).

```
every PriorityCalcPeriod (simulator clock):
  controller_step(now):
    if a schedule window covers now:          mode = OVERRIDE, want = window.policy   (no state sampling)
    elif now - last_switch < T_min:           mode = AUTO, cooldown → skip           (no state sampling — G4)
    else:                                     mode = AUTO, sample state → classify → want = policy_for(state)
    if want != active:
        swap weight vector; recompute priority of all pending jobs (one pass, job write lock)
        last_switch = now; log the switch
  normal multifactor recalculation continues
```

**Why in-plugin and not an external daemon:** the UB simulator runs slurmctld on a virtual, accelerated clock. An external process polling `squeue` would run on wall-clock time and race the simulation. Logic inside the plugin sees the same clock as the scheduler and is deterministic for a given trace.

**Testability:** the decision logic is written as a pure function `decide(snapshot, config, now, controller_state) → decision`, with no Slurm locks or globals, so it can be unit-tested outside slurmctld.

### 4.1 Config files (`ver_0.2/config/`)
The plugin reads these at startup and on `scontrol reconfigure`.

`policies.conf` (one policy per line, in slurm.conf-style key=value pairs):
```
PolicyName=F    Age=10000 Fairshare=80000 JobSize=0     Balance=0     TRES=CPU=5000,Mem=5000
PolicyName=B    Age=20000 Fairshare=10000 JobSize=0     Balance=70000 TRES=CPU=0,Mem=0
...
```

`schedule.conf` (override windows; overlapping windows are rejected at load):
```
Window Start=2024-04-08T12:00:00 End=2024-04-08T17:00:00 Policy=U      # one-off
Window Daily=12:00-17:00 Policy=U                                       # recurring (the trace's daily peak, UTC)
```

`adaptive.conf`:
```
TMin=60min
DefaultPolicy=BASE
Signal=fairness    Enter=0.40 Exit=0.30
Signal=balance     Enter=0.50 Exit=0.35
Signal=util        Enter=0.25 Exit=0.15
MinDemand=0.10
QueuePressureMin=0.5
LogFile=/path/decisions.csv
```
(These numbers are placeholders. Thresholds get calibrated in workflow phase P6.)

### 4.2 Decision log (CSV, one row per tick)
`sim_time, mode, eligible, sampled, s_fairness, s_balance, s_util, queue_pressure, u_cores, u_mem, active_policy, chosen_policy, switched, swap_us, pending_jobs`

When a switch happens, the plugin can optionally dump the pending queue's priority order just before and after it (used for churn analysis, §8).

---

## 5. Background constraint: keep priorities on one scale *(proposed)*
Factors are normalized to `[0,1]`, but weights are raw integers. If policies have different weight totals, a switch also rescales every priority, which skews comparisons and churn numbers. **Every policy's weights therefore sum to the same total (100,000).** Partition and QOS weights stay at 0, since the setup has a single partition and a single QOS.

## 6. Policies (G5)

Three metrics:
- **F, fairness:** equity across **users/accounts** relative to their shares.
- **B, balance:** even **load across resource types**, cores vs memory, so one isn't maxed out while the other sits idle with work waiting for it (§6.1).
- **U, utilization:** busy fraction of the cluster's **cores**.

Define three **component vectors** (each summing to 100k, values are starting points to tune in P4):

| Component | Age | Fairshare | JobSize | Balance | TRES CPU | TRES Mem |
|---|---|---|---|---|---|---|
| F | 10k | 80k | 0 | 0 | 5k | 5k |
| B | 20k | 10k | 0 | 70k | 0 | 0 |
| U | 10k | 0 | 40k | 0 | 40k | 10k |

Every policy is a convex mix of these three vectors, so all policies stay normalized automatically and only three vectors need tuning:

| Family | Policies | Mix coefficients |
|---|---|---|
| Single-metric | `F`, `B`, `U` | 1.0 on one component |
| Ordered composite (`X>Y>Z`) | `F>B>U`, `F>U>B`, `B>F>U`, `B>U>F`, `U>F>B`, `U>B>F` | 0.6 / 0.3 / 0.1 |
| Equal blend | `EQ` | 1/3 each |
| Reference / default | `BASE` | a typical stock site config, rescaled to 100k. Runs in auto mode whenever no signal is active, so auto equals the baseline until a trigger fires |

That makes 10 policies the controller switches to on a trigger, plus `BASE` as the resting state. *(Optional extension: a "strict tier" family with 100:10:1 ratios, to compare near-lexicographic ordering against blending.)*

### 6.1 Balance factor (new in `priority/adaptive`)
Stock Slurm factors always favor the same kind of job, but which resource type is underused changes over time. The plugin therefore adds one factor that reads current load:

```
for each resource type t in {cores, memory}:
    idle_t  = 1 − allocated_t / total_t        (snapshot once per recalculation pass)
    share_t = job's request of t / total_t
f_balance(job) = Σ_t share_t · idle_t  ÷  Σ_t share_t        (in [0,1])
```

- `f_balance` is the idle fraction of the resources the job asks for, weighted by how much of each it asks for.
- Every job competes for both cores and memory on the same nodes. A job that asks for lots of memory per core can fill a node's memory and strand its cores; a job that asks for many cores with little memory can do the reverse.
- When cores and memory are equally loaded, every job gets the same value, so the factor drops out and the other weights decide. Its effect grows with the imbalance.
- Example: half the cores idle, memory 90% allocated. A job asking for 16 cores + 16 GB scores about 0.42, and a job asking for 2 cores + 64 GB scores about 0.14, so the core-heavy job goes first.
- Idle fractions are taken once per recalculation pass, so a job submitted in between uses a snapshot at most one `PriorityCalcPeriod` old.
- `BASE` has weight 0 on this factor, so the G1 regression check still compares against stock multifactor.

**Why reordering is enough:** the highest-priority pending jobs get backfill reservations. When jobs that use the idle resource rise to the top, they hold start times on the nodes, and jobs that would strand it further can use those nodes only in gaps that end before then.

**Overlap with U:** when cores sit idle because memory is taken, both U (always leans toward big core consumers) and B (leans toward whatever is idle now) favor core-heavy jobs. The P4 check confirms the two behave differently; if they don't, U's TRES weights get retuned.

## 7. System states (G4)

All signals come from one in-memory snapshot of pending and running jobs, node state and association usage. Each signal is in `[0,1]`.

| Signal | Meaning | Proposed formula |
|---|---|---|
| `queue_pressure` (gate) | Is there contention at all? | pending requested core-hours ÷ (cluster cores × 1 h). Below `QueuePressureMin`, the state is NORMAL and no switch happens |
| `s_fairness` | One user or account dominates the waiting work | 1 − Jain index of per-user pending demand ÷ share, over users with pending jobs |
| `s_balance` | Cores and memory are unevenly loaded, and work is waiting for the less-used one | abs(u_cores − u_mem), where u is the allocated fraction. Counts only when pending demand for the less-used type is at least `MinDemand` of its capacity; otherwise 0 |
| `s_util` | The machine as a whole sits idle while work waits | average idle fraction over the resource types that have pending demand of at least `MinDemand` of their capacity |

**Classification (maps states onto the policy set):**
- Hysteresis: a signal becomes *active* when it crosses `Enter` and stays active until it falls below `Exit`.
- No active signal: `DefaultPolicy` (`BASE`).
- One active signal: that signal's single-metric policy (`F`, `B` or `U`).
- Two or more active: the ordered composite ranked by normalized severity `s / Enter`. For example, `s_F/θ_F > s_U/θ_U > s_B/θ_B` gives `F>U>B`.

**Sampling rule:** no snapshot is taken during cooldown or override, which keeps the controller cost at zero when it can't act anyway.

**Calibration:** `Enter` is set at the p90 and `Exit` at the p75 of each signal's distribution in static `BASE` runs of the general workload, using **calibration seeds only**. Evaluation uses held-out seeds.

## 8. Weight updates, overhead, and T_min (G3)

**Mechanism:** replace the active weight vector, then recompute every pending job's priority in a single pass under the job write lock.

**Overhead, measured at four levels:**
1. **Controller cost:** µs per swap and µs per state sample, against the number of pending jobs (scaling plot).
2. **Scheduler impact:** main and backfill cycle times (sdiag counters) just before and after each switch.
3. **Disruption:**
   - queue-order churn: Kendall-τ distance between pending order just before and after a switch
   - reservation churn: pending jobs whose expected start moved later
   - bumped jobs: jobs that were next in line but lost their place
4. **End-to-end cost of switching:** forced alternation between two very different policies (`F` and `U`) every *P* ∈ {1 tick, 15 m, 30 m, 1 h, 2 h, 4 h}, compared with each one run statically. This isolates what switching costs from whether the decisions were any good.

**Timing caveat:** the simulator virtualizes time. Phase P1 must confirm which clock (for example `CLOCK_MONOTONIC`) still reports real elapsed time, so that `swap_us` means something.

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
| `elapsed` | actual runtime the simulator replays | never above `timelimit` |
| `user`, `account` | user and account for fair-share | |
| `partition` | not kept | every job goes to the single partition |
| `state` | used only for filtering | failed and timed-out jobs still held resources, so they stay |
| `maxrss`, `totalcpu`, `maxdiskwrite`, `nsteps`, `el9` | not used | actual usage is out of scope (§3) |
| `hostnames` | used only to drop multi-node jobs | |

**Filters, in order:**
1. Keep partitions `htc`, `htc_highmem` and `hpc`. This drops `gpu` and `gpu_interactive` (no GPUs in this project), `htc_daemon` (service jobs with limits up to 90 days), and `htc_interactive` and `flash` (interactive and very small). About 54k jobs go.
2. Drop jobs that never started (about 287k, cancelled while pending) and jobs with zero elapsed time (about 331k).
3. Drop jobs that don't fit one node: more than 32 cores, more than 128 GB, or multi-node. That's 0.05% of jobs and 1.1% of core-hours.

About 22.9M jobs remain.

**Scaling by random job sampling:** each seed draws an independent random sample of the filtered trace, keeping each job with probability *p*. The converter sets *p* so the sample's average offered load is about 0.85 of 512 cores. That works out to roughly 3.3% on average, varying a little by month, computed exactly from the filtered trace for each window. Sampling keeps the real mix of job shapes, users and daily cycle. Bursts shrink in absolute size but keep their size relative to the cluster.

**What the trace says about these jobs** (these replace the earlier generator assumptions):
- **Sizes:** median 1 core, p90 2 cores, p99 12 cores.
- **Memory per core:** 61% of jobs ask for up to 4 GB per core (the node's own ratio), 30% for 4–8 GB, and 9% for more than 8 GB. Memory-heavy jobs are common, so balance has real work to do.
- **Requested vs actual time:** for completed jobs, the time limit is a median 11× the runtime, with a long tail (p75 above 300×).
- **User skew:** the top user accounts for 18% of core-hours and the top 10 users for 51%. The top account accounts for 20%.
- **Daily cycle:** the busiest 5 hours are 12:00–17:00 UTC, with 33% of submitted core-hours (an even spread would give 21%), about 1.6× the average rate. The day has a morning and an afternoon peak, so the 5-hour peak isn't sharp.

### 9.3 Workloads from real episodes
The trigger workloads are windows of the trace where the pattern happens on its own. Nothing is injected.

| ID | Workload | Purpose | How it's found *(proposed thresholds)* | Found in the full trace |
|---|---|---|---|---|
| W0 | General | baseline, false-trigger check | windows with none of the episodes below | — |
| W1 | User flood | fires **fairness** | a 6-hour window where one user submits over half the core-hours and the total is above 1.5× the median | 58 windows, 22 different users |
| W2 | Memory-heavy burst: memory-heavy jobs fill nodes' memory and strand their cores | fires **balance** | a 6-hour window where jobs above 8 GB per core make up more than 3× their median share of submitted core-hours | 91 episodes |
| W3 | Idle with backlog: cores idle while jobs wait, for example whole-node jobs at the head of the queue holding reservations | fires **utilization** | needs `BASE` simulation runs; found in P6 | not yet known |
| W4 | Sequence | several correct switches in one run | a window holding episodes of at least two kinds back to back | built from the W1–W3 lists |
| W5 | Oscillating: signals flip faster than a plausible T_min | tests cooldown and hysteresis | needs `BASE` simulation runs; found in P6 | not yet known; may not exist |
| W6 | Daily peak | override vs auto | the natural peak, 12:00–17:00 UTC, over several days | present in the daily profile (about 1.6× the average rate, not the ×2.5 first assumed) |

**Onset times:** each episode's estimated start is recorded in a sidecar file and used as ground truth for response latency.
- **W1 and W2:** the start of the burst's submissions, refined to the hour.
- **W3 and W5:** the first time the signal crosses `Enter` in the `BASE` run.

**Calibration vs evaluation** *(proposed)*: different seeds of the same window contain the same real events, just a different sample of jobs, so tuning and testing on one window would share those events. Calibration therefore uses windows from January–February, and evaluation uses windows from March–June. Both sets contain episodes; for example, the user floods split 21 to 37.

**Run length:** how long each window is depends on how fast the simulator runs. That gets set once P1 measures it.

**Warm-up:** the first stretch of each trace fills fair-share usage history and is excluded from metrics. Metrics cover only jobs submitted inside the measured window.

## 10. Evaluation (G7)

**Metrics**, computed from job completion records plus the decision log:
- **Fairness:** Jain index over per-user mean bounded slowdown; worst-user / best-user mean wait.
- **Balance:** time-averaged gap between core and memory utilization; stranded core-hours (cores idle while jobs wait because their node's memory is taken).
- **Utilization:** core utilization over the measured window (memory utilization reported alongside); makespan.
- **General:** mean, median, p95 and max wait; mean bounded slowdown (τ = 10 s); throughput.
- **Overhead:** switch count, `swap_us`, churn.

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
| 1 | Simulator | Newer UB simulator, built on Slurm 23.x |
| 2 | What "balance" means | Even load across resource types (cores vs memory), steered by a new balance factor in the plugin (§6.1). Spreading jobs across individual nodes was ruled out: that's placement, which Slurm's node selection decides, not priority |
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

**Still open:**
- Exact Slurm 23.x release inside the simulator: confirmed in P1.
- Whether the simulator tracks memory as a consumable resource: confirmed in P1. Balance as defined needs it.
- `MinDemand` for the balance and utilization signals: calibrated in P6 along with the thresholds.
- Episode-finding thresholds for W1 and W2 (§9.3) are first-pass proposals.
- W3 and W5 can only be found from `BASE` simulation runs (P6). If W5 turns out not to exist in the trace, what to do about it needs a decision then.
- Month split for calibration (January–February) vs evaluation (March–June): proposed.
- Window length per run: set once P1 measures simulator speed.
- X and Y for the T_min rule: set after P5.
- Non-goals marked *(proposed)* in §3, and the §5 normalization rule: need sign-off.

## 12. Repository layout (all inside `ver_0.2/`)
```
SPEC.md  WORKFLOW.md  PROGRESS.md
docker/                      simulator image with priority/adaptive compiled in
plugin/priority_adaptive/    C source + build glue
plugin/tests/                unit tests for decide()
config/                      slurm.conf, policies.conf, schedule.conf, adaptive.conf
dataset/                     source trace (read-only, too large to commit)
workloads/convert/           trace filter, random-job sampler, simulator converter
workloads/episodes/          episode finder + onset sidecars
workloads/traces/            sampled, converted traces
experiments/                 run-matrix definitions + runner
analysis/                    metrics + plots
results/                     raw per-run output + summary tables
```
