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
| G6 | Simulated workloads: general, trigger patterns, a 5-hour peak window | Generated traces hit their target offered load, and trigger onset times are recorded as ground truth |
| G7 | Evaluate with the UB Slurm simulator | Results table and plots regenerate from one command, with confidence intervals across seeds |

## 3. Non-goals
- No prediction of job resources or runtimes. The scheduler uses only requested values. (Workloads still give jobs actual runtimes at or below the requested walltime, as real traces do.)
- *(proposed)* Only priority weights change at runtime. Backfill parameters, the select plugin, partitions, QOS, `PriorityFlags` and decay settings stay fixed.
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
PolicyName=F    Age=10000 Fairshare=80000 JobSize=0     Balance=0     TRES=CPU=5000,GRES/gpu=5000
PolicyName=B    Age=20000 Fairshare=10000 JobSize=0     Balance=70000 TRES=CPU=0,GRES/gpu=0
...
```

`schedule.conf` (override windows; overlapping windows are rejected at load):
```
Window Start=2026-01-05T10:00:00 End=2026-01-05T15:00:00 Policy=U      # one-off
Window Daily=10:00-15:00 Policy=U                                       # recurring
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
`sim_time, mode, eligible, sampled, s_fairness, s_balance, s_util, queue_pressure, u_cpu_cores, u_gpu_cores, u_gpu, active_policy, chosen_policy, switched, swap_us, pending_jobs`

When a switch happens, the plugin can optionally dump the pending queue's priority order just before and after it (used for churn analysis, §8).

---

## 5. Background constraint: keep priorities on one scale *(proposed)*
Factors are normalized to `[0,1]`, but weights are raw integers. If policies have different weight totals, a switch also rescales every priority, which skews comparisons and churn numbers. **Every policy's weights therefore sum to the same total (100,000).** Partition and QOS weights stay at 0: the two partitions (§9) share one priority tier, and there's a single QOS.

## 6. Policies (G5)

Three metrics:
- **F, fairness:** equity across **users/accounts** relative to their shares.
- **B, balance:** even **load across resource types**, so one type isn't maxed out while another sits idle with work waiting for it. Since CPU-only jobs are kept off GPU nodes, this plays out on the GPU nodes: their cores vs their GPUs (§6.1).
- **U, utilization:** busy fraction of **devices** (CPU cores and GPUs).

Define three **component vectors** (each summing to 100k, values are starting points to tune in P4):

| Component | Age | Fairshare | JobSize | Balance | TRES CPU | TRES GPU |
|---|---|---|---|---|---|---|
| F | 10k | 80k | 0 | 0 | 5k | 5k |
| B | 20k | 10k | 0 | 70k | 0 | 0 |
| U | 10k | 0 | 40k | 0 | 20k | 30k |

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
for each resource type t in {CPU-node cores, GPU-node cores, GPUs}:
    idle_t  = 1 − allocated_t / total_t        (snapshot once per recalculation pass)
    share_t = job's request of t / total_t
f_balance(job) = Σ_t share_t · idle_t  ÷  Σ_t share_t        (in [0,1])
```

- `f_balance` is the idle fraction of the resources the job asks for, weighted by how much of each it asks for.
- CPU-only jobs never run on GPU nodes (§9), so CPU-only and GPU jobs don't compete for the same resources, and the order between them barely matters. The factor does its work **on the GPU nodes**, where GPU jobs compete for both cores and GPUs: a job that asks for many cores per GPU can fill a node's cores and strand its GPUs.
- A CPU-only job touches only CPU-node cores, so its score is just their idle fraction.
- When the GPU nodes' cores and GPUs are equally loaded, every GPU job gets the same value, so the factor drops out and the other weights decide. Its effect grows with the imbalance.
- Example: GPU-node cores 90% busy, half the GPUs idle. A job asking for 4 GPUs + 8 cores scores about 0.37, and a job asking for 1 GPU + 32 cores scores about 0.14, so the GPU-heavy job goes first.
- Idle fractions are taken once per recalculation pass, so a job submitted in between uses a snapshot at most one `PriorityCalcPeriod` old.
- `BASE` has weight 0 on this factor, so the G1 regression check still compares against stock multifactor.

**Why reordering is enough:** the highest-priority pending jobs get backfill reservations. When GPU-heavy jobs rise to the top, they hold start times on the GPU nodes, and core-heavy GPU jobs can use those nodes only in gaps that end before then.

**Overlap with U:** when GPUs sit idle on the GPU nodes, both U (always leans toward big consumers) and B (leans toward whatever is idle now) favor GPU-heavy jobs. The P4 check confirms the two behave differently; if they don't, U's TRES weights get retuned.

## 7. System states (G4)

All signals come from one in-memory snapshot of pending and running jobs, node state and association usage. Each signal is in `[0,1]`.

| Signal | Meaning | Proposed formula |
|---|---|---|
| `queue_pressure` (gate) | Is there contention at all? | pending requested core-hours ÷ (cluster cores × 1 h). Below `QueuePressureMin`, the state is NORMAL and no switch happens |
| `s_fairness` | One user or account dominates the waiting work | 1 − Jain index of per-user pending demand ÷ share, over users with pending jobs |
| `s_balance` | On the GPU nodes, cores and GPUs are unevenly loaded, and work is waiting for the less-used one | abs(u_gpu_cores − u_gpu), where u is the allocated fraction on the GPU nodes. Counts only when pending demand for the less-used type is at least `MinDemand` of its capacity; otherwise 0 |
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
- 16 CPU nodes × 32 cores.
- 3 GPU nodes × 8 GPUs (24 GPUs total), 32 cores each *(assumed core count)*.
- Two partitions, `cpu` (the 16 CPU nodes) and `gpu` (the 3 GPU nodes), and one QOS. CPU-only jobs go to `cpu` and GPU jobs to `gpu`, so **CPU-only jobs never run on GPU nodes**.
- 40 users in 8 accounts with equal shares. Per-user activity is skewed (Zipf), so a few users are heavy.

**Generator:** a seeded Python tool that writes a neutral trace (`submit_time, user, account, nodes, cores, gpus, req_walltime, runtime`) and a converter into the UB simulator's job-trace format.
- Arrivals: Poisson with rate λ(t).
- Sizes: power-of-two core counts skewed small. GPU jobs also get a GPU count and a cores-per-GPU ratio, which drives the balance tests.
- Runtimes: lognormal.
- Requested walltime: runtime × U(1, 5), rounded up to standard limits.
- User activity: Zipf.
- Load target, as offered load: ρ = Σ(cores × runtime) ÷ (capacity × duration).

| ID | Workload | Purpose |
|---|---|---|
| W0 | General: steady ρ ≈ 0.85, mild day/night cycle, several days | baseline, false-trigger check |
| W1 | User flood: W0 plus one user bursts many jobs at t₀ | fires **fairness** |
| W2 | Resource imbalance: a burst of core-heavy GPU jobs (many cores per GPU) fills the GPU nodes' cores and strands GPUs while GPU-heavy jobs queue | fires **balance** |
| W3 | Idle with backlog: wide, long jobs at the head of the queue leave cores idle while smaller jobs wait | fires **utilization** |
| W4 | Sequence: W1 → W2 → W3 segments | several correct switches in one run |
| W5 | Oscillating: triggers alternate faster than a plausible T_min | tests cooldown and hysteresis |
| W6 | 5-hour peak: W0 base with arrival rate ×2.5 from 10:00 to 15:00 daily, several days | override vs auto |

Each trace ships with a sidecar file of trigger onset times, used as ground truth for response latency.

**Warm-up:** the first stretch of each trace fills fair-share usage history and is excluded from metrics. Metrics cover only jobs submitted inside the measured window.

## 10. Evaluation (G7)

**Metrics**, computed from job completion records plus the decision log:
- **Fairness:** Jain index over per-user mean bounded slowdown; worst-user / best-user mean wait.
- **Balance:** time-averaged gap between core and GPU utilization on the GPU nodes; stranded GPU-hours (GPUs idle while GPU jobs wait because their node's cores are taken).
- **Utilization:** CPU and GPU utilization over the measured window; makespan.
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

**Seeds:** each workload is generated with 3 calibration seeds (used for P4 tuning and P6 thresholds) and 5 evaluation seeds (used only for reported results).

Report every metric as mean ± 95% CI across the 5 evaluation seeds. All plots come from one script reading raw results.

---

## 11. Decisions and remaining open items

**Decided:**

| # | Question | Decision |
|---|---|---|
| 1 | Simulator | Newer UB simulator, built on Slurm 23.x |
| 2 | What "balance" means | Even load across resource types (cores vs GPUs), steered by a new balance factor in the plugin (§6.1). Spreading jobs across individual nodes was ruled out: that's placement, which Slurm's node selection decides, not priority. CPU-only jobs are kept off GPU nodes, so the factor acts on the GPU nodes' cores vs GPUs |
| 3 | Override vs T_min | Override wins; a window's end starts a fresh cooldown |
| 4 | Device utilization | CPUs and GPUs |
| 5 | Cluster and users | 16 CPU nodes × 32 cores, 3 GPU nodes × 8 GPUs, in separate `cpu` and `gpu` partitions so CPU-only jobs never run on GPU nodes; 40 users in 8 equal-share accounts |
| 6 | Default policy in auto | `BASE` |
| 7 | Seeds | 3 calibration + 5 evaluation per workload |
| 8 | T_min rule numbers (X, Y) | Set after P5's forced-switch results, before the T_min sweep |
| 9 | `scontrol reconfigure` comparison | Stretch goal, after the main evaluation |

**Still open:**
- Exact Slurm 23.x release inside the simulator: confirmed in P1.
- Core count per GPU node: assumed 32 (§9). It sets how easily core-heavy GPU jobs strand GPUs, so the balance tests depend on it.
- `MinDemand` for the balance and utilization signals: calibrated in P6 along with the thresholds.
- X and Y for the T_min rule: set after P5.
- Non-goals marked *(proposed)* in §3, and the §5 normalization rule: need sign-off.

## 12. Repository layout (all inside `ver_0.2/`)
```
SPEC.md  WORKFLOW.md  PROGRESS.md
docker/                      simulator image with priority/adaptive compiled in
plugin/priority_adaptive/    C source + build glue
plugin/tests/                unit tests for decide()
config/                      slurm.conf, policies.conf, schedule.conf, adaptive.conf
workloads/gen/               generator + converter
workloads/traces/            generated traces + trigger sidecars
experiments/                 run-matrix definitions + runner
analysis/                    metrics + plots
results/                     raw per-run output + summary tables
```
