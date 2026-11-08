# Development Plan: Multi-Policy Slurm Scheduler

Based on [SPEC.md](SPEC.md).

**Objective:** Build a Slurm scheduler that switches between policies during a run based on cluster state, with each policy maximizing a different metric. Evaluate it on a 24-GPU simulated cluster using the University at Buffalo Slurm Simulator.

---

## Phase 0: Decisions to Make First

These choices affect everything after them, so settle them before writing code.

> **What a "run" is:** one execution of the simulator. A single workload (a list of jobs with submit times, GPU counts and runtimes) goes through the simulated 24-GPU cluster under one scheduler setup. The setup is either one fixed policy, or a starting policy plus any scheduled switches. The run starts at the first job submission and ends once every job has finished. Its output is a job log, plus a log of policy switches, from which all the metrics are computed. Comparing setups means doing several runs with the same workload, changing only the setup.

| # | Decision | Options | Notes |
|---|---|---|---|
| D1 | **How a policy is implemented** ✅ *Decided* | **Slurm config profiles:** each policy is a file of standard Slurm settings in `sim/etc/policies/<name>.conf`. No custom scheduling plugin. | Policies switch during a run (D2), so a profile may only use settings that can change mid-run. **Phase 1 verified these:** priority weights (`PriorityWeightAge`, `Fairshare`, `JobSize`, `Partition`, `QOS`), `PriorityFavorSmall`, node `Weight` and partition `LLN`. Anything else, such as `SchedulerParameters` or `SelectTypeParameters`, is reported and ignored at a switch. The simulator can't run `scontrol` (see D2b), so profiles are applied by the `switch_policy` event instead. |
| D2 | **What "change policies" means** ✅ *Decided* | **Switched during a run:** the scheduler changes policy partway through a single simulation based on cluster state, e.g. moving to the performance policy when the queue gets long. | Fixed policies (one per run) are still needed as the comparison points in Phase 4. |
| D2b | **Where the switching controller runs** ✅ *Revised after Phase 1* | **Inside the simulator:** a new `switch_policy` event type in the simulator's events file, added by our patch (`sim/patches/switch_policy.patch`). At its simulated time, it applies a policy profile inside `slurmctld`. | The original choice, an external script using `squeue`/`scontrol`, can't work. During a run the simulator never starts the thread that answers client commands, so every command fails with "Unable to contact slurm controller" (tested in Phase 1). The new event runs on the simulator's own clock, so switches land at exactly the scheduled time. Rule-driven switching later means a decision hook in the simulator's event loop (C), not a script. |
| D3 | **How the GPUs are split across nodes** ✅ *Decided* | **24 GPUs on 3 identical nodes:** `gpu1`, `gpu2` and `gpu3` with 8 GPUs each | See the cluster layout below. |
| D4 | **Workloads** ✅ *Decided* | **Synthetic job mixes first,** made by a workload generator. Real traces (e.g. from the Parallel Workloads Archive) may be added later. | Workloads are stored in one common format, so a real trace later only needs a converter into that format. See the workloads section below. |

### Cluster layout (D3)

| Node | GPUs | Share of cluster |
|---|---|---|
| `gpu1` | 8 | 33% |
| `gpu2` | 8 | 33% |
| `gpu3` | 8 | 33% |
| **Total** | **24** | |

**What this layout means for scheduling:**
- **Where a job can fit on a single node:** jobs needing 1–8 GPUs fit on any node. Jobs needing 9–16 GPUs span two nodes, and jobs needing 17–24 GPUs need all three.
- **Fragmentation is the main point of contention.** If small jobs are spread across all three nodes, free GPUs end up scattered. For example, with 4 free on each node, 12 GPUs are free but an 8-GPU job can't start on one node. *Packing* (filling one node before using the next) keeps whole nodes free for large jobs. *Spreading* (balancing load) does the opposite. That tension between the utilization and load-balance policies is what the switching mechanism moves between.
- **Load balance can use raw counts.** All nodes are the same size, so the spread of GPUs in use across the three nodes measures balance directly.
- **Slurm's least-loaded-node option (`LLN`) picks nodes by idle CPUs, not idle GPUs.** Every job gets a fixed number of CPUs per GPU (`DefCpuPerGPU=8`), so CPU load tracks GPU load. Each node has enough CPUs and memory that they don't become the bottleneck instead of the GPUs. If CPU-based balancing still doesn't follow GPU load, the load-balance policy would need a plugin, which is outside the config-only approach in D1.

**Config as built in Phase 1:** see [sim/etc/slurm.conf](sim/etc/slurm.conf). Each node has 64 cores and 512 GB, which is 8 cores and 64 GB per GPU. The node and partition lines live in the policy files, because they carry the settings policies change (`Weight`, `LLN`).

For packing, a policy can give the nodes different `Weight` values (e.g. 1, 2, 3). Slurm allocates lower-weight nodes first, so it fills `gpu1` before moving on to `gpu2`. Phase 1 confirmed that `Weight` changes take effect mid-run.

### Workloads (D4)

**Common format.** Every workload is one file with a row per job: `job_id, user, submit_time, gpus, runtime, time_limit`. A converter turns that file into the simulator's input format (an "events file"; see [sim/README.md](sim/README.md) for the job request form that works). The generator writes this format now, and a real trace later only needs a converter into it. Keep `time_limit` separate from `runtime`, because backfill plans with the time limit while the job actually ends at its runtime.

**Generator parameters:**

| Parameter | What it controls |
|---|---|
| `seed` | Makes a workload reproducible: same seed, same jobs |
| Number of jobs or simulated duration | How long the run is |
| Arrival rate (with optional bursts) | How heavily loaded the cluster is |
| GPU-count distribution (1–24) | Mix of single-node, two-node and three-node jobs |
| Runtime distribution, and time limit as a multiple of runtime | Job lengths, and how accurate users' time estimates are |
| Users and their share of jobs | What the fairness policy has to work with |
| Phases | Changing any of the above partway through a run, so the job mix shifts |

**Starter set:**

| Workload | Purpose |
|---|---|
| Light load | Sanity check: queue mostly empty, so every policy should give the same result |
| Heavy load, mostly small jobs (1–4 GPUs) | Fragmentation: packing vs. spreading |
| Heavy load, mixed sizes including 8-GPU and multi-node jobs | Contention for whole nodes |
| Shifting mix (many short small jobs, then long large ones) | Makes scheduled switches visible now, and gives switching rules something to react to later |
| Skewed users (one heavy user, several light ones) | Fairness |

**Real traces later.** Write a converter from the Parallel Workloads Archive's Standard Workload Format (SWF) into the common format. Most archived traces are CPU jobs, so the converter also has to map processor counts to GPU counts and scale the load to 24 GPUs.

---

## Phase 1: Environment Setup

**Goal:** Get a working UB Slurm Simulator running a 24-GPU cluster.

**Status: done.** Details are in [sim/README.md](sim/README.md).

- [x] Set up the UB Slurm Simulator v3.0 from the prebuilt image `nsimakov/slurm_sim:v3.0`. Its docs say Slurm 23.02, but `slurmctld` reports **23.11.0-0rc1**, so use the 23.11 docs.
- [x] Run the simulator's bundled example workload from start to finish (micro-cluster tutorial: 20 jobs).
- [x] Define the 24-GPU cluster in `slurm.conf` and `gres.conf`, using GPUs declared as a count only.
- [x] Write a 20-job test workload ([sim/workloads/test20.events](sim/workloads/test20.events)) covering 1–8, 12–16 and 24 GPU jobs and one timeout. All 20 jobs scheduled correctly. Jobs over 8 GPUs spanned 2 or 3 nodes, with 8 CPUs per GPU.
- [x] Script the full run loop: `scripts/run_sim.sh <events-file>`. It runs about 200x faster than real time, e.g. 1 h 49 min of simulated time in 33 s.
- [x] Test changing scheduler settings mid-run. `scontrol` doesn't work during a simulation, so instead we added a `switch_policy` event (D2b). Verified with [sim/workloads/test_switch.events](sim/workloads/test_switch.events):
  - node `Weight`: new jobs moved to the lowest-weight node;
  - partition `LLN`: new jobs went to the idle node;
  - priority weights: after switching, a queued 1-GPU job overtook a queued 24-GPU job submitted before it.
- [x] ~~Test the external script approach (D2b).~~ Not possible: `squeue`, `sinfo` and `scontrol` all fail during a run. Replaced by the `switch_policy` event, which lands at exactly the scheduled simulated time.

**Done when:** One command runs a workload on the 24-GPU simulated cluster and produces a job log. ✅

---

## Phase 2: Metrics and Baseline

**Goal:** Be able to measure every metric before changing the scheduler.

**Status: done.** How to run it is in [sim/README.md](sim/README.md#workloads-and-metrics).

- [x] Write an analysis script that reads the simulator's job output (`sacct`-style records) and computes these metrics. Done: [scripts/metrics.py](scripts/metrics.py), which also rebuilds per-node GPU use by replaying job starts and ends:

| Objective | Metric(s) |
|---|---|
| Utilization | GPU-hours used ÷ GPU-hours available; idle GPU time |
| Performance | Average and p95 wait time, turnaround time, makespan, bounded slowdown |
| Stability / load balance | Variance of GPUs in use across the three nodes; variance in wait time |
| Fairness | Jain's fairness index over per-user slowdown or share of GPU-hours |

- [x] Write the converter from the common workload format into the simulator's input format. Test it on the hand-written Phase 1 workload. Done: [scripts/convert_workload.py](scripts/convert_workload.py); converting [test20.csv](sim/workloads/test20.csv) gives a file identical to the hand-written `test20.events`. `--switch SECONDS:POLICY` adds scheduled switches to any workload. Phase 1 findings to follow:
  - Request GPUs as `-N <fewest nodes> --gpus=<G>`. The simulator rejects `--gpus-per-task`, and without `-N` a 4-GPU job can be split across nodes.
  - Use `-sim-walltime -1` for a job that runs until its time limit. A walltime of `0` ends the job instantly.
  - `slurm_acct.out` does not list GPUs per job, so the metrics script must take GPU counts from the workload file.
- [x] Write the workload generator with the parameters above. The same seed must always produce the same file. Done: [scripts/generate_workload.py](scripts/generate_workload.py), driven by JSON specs in [sim/workloads/specs/](sim/workloads/specs/). Arrival rate is set as a target offered load.
- [x] Set up the users and accounts in Slurm's accounting database (`sacctmgr`) so fair-share has users to work with. Done in Phase 1: 5 users in 3 accounts ([sim/etc/sacctmgr.script](sim/etc/sacctmgr.script), [sim/etc/users.sim](sim/etc/users.sim)). Extend them if the starter workloads need more users.
- [x] Generate the starter set of five workloads: `light`, `heavy_small`, `heavy_mixed`, `shifting`, `skewed_users` (99–665 jobs each, 12 h of submissions; `shifting` 18 h).
- [x] Run the default Slurm scheduler (FIFO plus backfill) on every workload as the **baseline**. Done: `scripts/run_baseline.sh` regenerates, runs and measures all five; about 10 minutes in total.
- [x] Save the results in a consistent format such as CSV or JSON. Done: [results/baseline.csv](results/baseline.csv) (one row per run) and `results/baseline/<workload>.metrics.json` (per user and per node).

**Done when:** The starter workloads can be regenerated from their seeds, and the baseline results for every metric come out of a script, not hand calculation. ✅

---

## Phase 3: Policies and Switching

**Goal:** One policy for each objective, plus the mechanism to switch between them during a run.

### 3a. Individual policies

| Policy | Config profile |
|---|---|
| **Utilization** | Aggressive backfill; pack jobs onto as few nodes as possible (node `Weight` sets the fill order) so whole nodes stay free for 8-GPU and multi-node jobs |
| **Performance** | Prioritize short or small jobs (job size and age weights) to cut average wait and slowdown, while still letting large jobs through so they don't wait indefinitely |
| **Load balance** | Spread jobs so the three nodes have about the same number of GPUs in use (partition `LLN=YES` with `DefCpuPerGPU`) |
| **Fairness** | Weight priority heavily on fair-share, with a decay half-life suited to the length of the workload |

- [ ] Write each policy as a config profile in `sim/etc/policies/<name>.conf`, following [baseline.conf](sim/etc/policies/baseline.conf). Use only the settings Phase 1 showed can change mid-run: priority weights, `PriorityFavorSmall`, node `Weight` and partition `LLN`.
- [x] Add a way to run a single fixed policy for comparison in Phase 4. Done in Phase 1: `SIM_START_POLICY=<name> scripts/run_sim.sh ...` starts from any policy, and with no switch events it stays there.
- [ ] Sanity-check each fixed policy on the small test workload. It should beat the baseline on its own metric.

### 3b. Switching mechanism

Build the machinery for switching policy during a run, but **not the rules that decide when to switch**. Those come later. They plug into the decision hook below without changing the rest of the mechanism.

Everything here lives inside the simulator (D2b): C code in `sim/patches/switch_policy.patch`, built into the image by [docker/Dockerfile](docker/Dockerfile).

- [x] **Policy switcher:** `sim_switch_policy(name)` in the patch applies any profile to the running scheduler. Built in Phase 1.
- [ ] **State snapshot:** at each check, collect cluster state into one record. This includes pending jobs, GPUs requested by pending jobs, GPUs in use on each node and the wait times of pending jobs. Rules will read from it later. For now, just log it.
- [ ] **Decision hook:** a function called at each check, e.g. `choose_policy(state, current_policy) -> policy`. For now it always returns the current policy, so nothing switches on its own. When it returns a different policy, it calls `sim_switch_policy`.
- [ ] **Check interval:** how often the snapshot and hook run, in simulated time. Make it configurable (e.g. a key in `sim.conf`) and schedule it as a recurring simulator event, the way priority decay already is.
- [x] **Test triggers:** `-dt <seconds> -e switch_policy | <name>` lines in the events file. Built in Phase 1.
- [x] **Switch log:** each run writes `policy_switches.csv` (simulated time, old policy, new policy, status). Built in Phase 1. Add a reason column when the decision hook exists ("scheduled" vs. "rule").

**Done when:** Each fixed policy beats the baseline on its own metric. A test run with scheduled triggers switches policy at the set times, the switch log records each one, and jobs scheduled after a switch follow the new policy.

**Later (not in this plan):** switching rules, i.e. which state conditions trigger which policy, and protection against switching back and forth too often.

---

## Phase 4: Evaluation

**Goal:** Compare the policies fairly.

- [ ] Run every workload under each setup: the baseline and each of the four fixed policies. Repeat runs if the simulator has randomness.
- [ ] Produce comparison tables and charts: each setup against each metric.
- [ ] Analyze the trade-offs, i.e. what each fixed policy gives up to gain its own metric.
- [ ] Check the switching mechanism: run workloads with scheduled switches, and confirm in the job log that scheduling behavior changes at each switch time and that the metrics before and after a switch reflect the active policy.

**Done when:** You have a full results matrix and charts that answer which policy wins on which metric and at what cost, and a demonstration that switching during a run works.

---

## Phase 5: Write-Up

- [ ] Document the architecture, how each policy is built and how to reproduce the results.
- [ ] Final report and presentation.

---

## Suggested Timeline

| Week | Phase | Milestone |
|---|---|---|
| 1 | 0 + 1 | Decisions made; simulator running the 24-GPU cluster ✅ |
| 2 | 2 | Workload generator, starter workloads, metrics script and baseline results |
| 3 | 3a | All four fixed policies implemented |
| 4 | 3b | Switching mechanism working with scheduled test switches |
| 5 | 4 | Evaluation runs and charts |
| 6 | 5 | Report and presentation |

Adjust the weeks to your actual deadline.

---

## Risks

| Risk | Mitigation |
|---|---|
| The simulator behaves differently from current Slurm docs | It reports Slurm 23.11.0-0rc1 (Phase 1). Use the 23.11 documentation |
| ~~GPU support in the simulator is limited~~ | Resolved in Phase 1: GPU jobs schedule correctly with `--gpus` / `--gres` |
| A policy needs a setting the switch can't change (e.g. backfill parameters) | The switch supports priority weights, `PriorityFavorSmall`, node `Weight` and partition `LLN`. Simplify the policy to those, or extend `sim_switch_policy` for one more setting |
| Workloads too small to show differences between policies | Increase load until the queue stays non-empty, so policies actually have choices to make |
| Our patch changes simulator behavior beyond the switch | It only adds an event type. Runs without switch events behave as before. Re-run the bundled tutorial after any patch change to check |
| A simulation never ends (e.g. every job rejected at submit) | `scripts/run_sim.sh` stops a run after `SIM_MAX_REAL_SECONDS` (default 1800) and fails if `slurmctld` logged a fatal error or no jobs were recorded |
---

## Gaps in the Spec

These sections of [SPEC.md](SPEC.md) are still empty and would make the plan sharper:
- **Requirements:** for example, how many workloads, and how long each simulated run should be.
- **Success criteria:** for example, how much a policy must improve over the baseline.
- **Constraints:** the deadline, the team size and who owns which phase.
