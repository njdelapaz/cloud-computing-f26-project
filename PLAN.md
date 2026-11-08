# Development Plan: Multi-Policy Slurm Scheduler

Based on [SPEC.md](SPEC.md).

**Objective:** Build a Slurm scheduler that switches between policies during a run based on cluster state, with each policy maximizing a different metric. Evaluate it on a 24-GPU simulated cluster using the University at Buffalo Slurm Simulator.

---

## Phase 0: Decisions to Make First

These choices affect everything after them, so settle them before writing code.

> **What a "run" is:** one execution of the simulator. A single workload (a list of jobs with submit times, GPU counts and runtimes) goes through the simulated 24-GPU cluster under one scheduler setup. The setup is either one fixed policy, or a starting policy plus any scheduled switches. The run starts at the first job submission and ends once every job has finished. Its output is a job log, plus a log of policy switches, from which all the metrics are computed. Comparing setups means doing several runs with the same workload, changing only the setup.

| # | Decision | Options | Notes |
|---|---|---|---|
| D1 | **How a policy is implemented** ✅ *Decided* | **Slurm config profiles:** each policy is a named set of standard Slurm settings, e.g. priority weights in `priority/multifactor`, backfill parameters, partition options and node `Weight`. No custom C code. | Because policies switch during a run (D2), a profile may only use settings that can change while `slurmctld` is running. Priority weights reload with `scontrol reconfigure`, and partition and node options change with `scontrol update`. `SelectTypeParameters` (where the cluster-wide `CR_LLN` lives) needs a restart, so spreading has to come from the partition-level `LLN` option instead. Phase 1 confirms which settings change live. A custom plugin is a fallback only if a policy can't be expressed with those settings. |
| D2 | **What "change policies" means** ✅ *Decided* | **Switched during a run:** the scheduler changes policy partway through a single simulation based on cluster state, e.g. moving to the performance policy when the queue gets long. | Fixed policies (one per run) are still needed as the comparison points in Phase 4. |
| D2b | **Where the switching controller runs** ✅ *Decided* | **External script:** a separate process that runs alongside the simulator. It reads cluster state with `squeue` and `sinfo` and applies policy profiles with `scontrol`. | This fits D1, since the profiles are applied with `scontrol` anyway, and it needs no changes to Slurm itself. The script has to time its checks and switches by the simulator's clock, not the wall clock. Phase 1 confirms that this works. |
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
- **Slurm's least-loaded-node option (`LLN`) picks nodes by idle CPUs, not idle GPUs.** Give every job a fixed number of CPUs per GPU (`DefCpuPerGPU`) so CPU load tracks GPU load. Give each node enough CPUs and memory that they don't become the bottleneck instead of the GPUs. If CPU-based balancing still doesn't follow GPU load, the load-balance policy would need a plugin, which is outside the config-only approach in D1.

**Starting config sketch** (CPU, memory and CPUs-per-GPU values are placeholders):

```
# slurm.conf
GresTypes=gpu
NodeName=gpu[1-3] Gres=gpu:8 CPUs=64 RealMemory=512000
PartitionName=gpu Nodes=gpu[1-3] Default=YES State=UP DefCpuPerGPU=8

# gres.conf
NodeName=gpu[1-3] Name=gpu Count=8
```

For packing, the utilization policy can give the nodes different `Weight` values (e.g. 1, 2, 3). Slurm allocates lower-weight nodes first, so it fills `gpu1` before moving on to `gpu2`. Whether `Weight` can change mid-run (with `scontrol update NodeName=...`) is one of the Phase 1 checks.

### Workloads (D4)

**Common format.** Every workload is one file with a row per job: `job_id, user, submit_time, gpus, runtime, time_limit`. A converter turns that file into the simulator's input format. The generator writes this format now, and a real trace later only needs a converter into it. Keep `time_limit` separate from `runtime`, because backfill plans with the time limit while the job actually ends at its runtime.

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

- [ ] Clone and build the UB Slurm Simulator (it has a Docker setup). Record which Slurm version it is based on.
- [ ] Run the simulator's bundled example workload from start to finish.
- [ ] Define the 24-GPU cluster in `slurm.conf` and `gres.conf`, using GPUs declared as a count only (no device files).
- [ ] Write a small test workload of about 20 jobs with different GPU counts and runtimes. Include 1–8 GPU jobs (fit on one node), 9–16 GPU jobs (span two nodes) and at least one job needing 17–24 GPUs (spans all three). Confirm that every size gets scheduled correctly.
- [ ] Script the full run loop (config → workload → simulate → collect output) so a single command reproduces a run.
- [ ] Test changing scheduler settings in the middle of a simulated run: priority weights with `scontrol reconfigure`, partition options and node `Weight` with `scontrol update`. Include the partition `LLN` option. Record which settings take effect live. The answers set which settings the policy profiles may use (D1).
- [ ] Test the external script approach (D2b): from a separate process, read the simulator's current simulated time, run `squeue`/`sinfo`, and apply an `scontrol` change at a chosen simulated time. Check how far the change lands from the intended time.

**Done when:** One command runs a workload on the 24-GPU simulated cluster and produces a job log.

---

## Phase 2: Metrics and Baseline

**Goal:** Be able to measure every metric before changing the scheduler.

- [ ] Write an analysis script that reads the simulator's job output (`sacct`-style records) and computes these metrics:

| Objective | Metric(s) |
|---|---|
| Utilization | GPU-hours used ÷ GPU-hours available; idle GPU time |
| Performance | Average and p95 wait time, turnaround time, makespan, bounded slowdown |
| Stability / load balance | Variance of GPUs in use across the three nodes; variance in wait time |
| Fairness | Jain's fairness index over per-user slowdown or share of GPU-hours |

- [ ] Write the converter from the common workload format into the simulator's input format. Test it on the hand-written Phase 1 workload.
- [ ] Write the workload generator with the parameters above. The same seed must always produce the same file.
- [ ] Set up the users and accounts in Slurm's accounting database (`sacctmgr`) so fair-share has users to work with.
- [ ] Generate the starter set of five workloads.
- [ ] Run the default Slurm scheduler (FIFO plus backfill) on every workload as the **baseline**.
- [ ] Save the results in a consistent format such as CSV or JSON.

**Done when:** The starter workloads can be regenerated from their seeds, and the baseline results for every metric come out of a script, not hand calculation.

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

- [ ] Write each policy as a config profile: a file listing the settings it changes, e.g. `policies/utilization.conf`. Use only settings that Phase 1 showed can change mid-run.
- [ ] Add a way to run a single fixed policy, such as a script flag (`--policy utilization`), for comparison in Phase 4.
- [ ] Sanity-check each fixed policy on the small test workload. It should beat the baseline on its own metric.

### 3b. Switching mechanism

Build the machinery for switching policy during a run, but **not the rules that decide when to switch**. Those come later. They plug into the decision hook below without changing the rest of the mechanism.

- [ ] **Policy switcher:** one entry point, e.g. `apply_policy(name)`, that applies any of the four policies to the running scheduler.
- [ ] **State snapshot:** at each check, collect cluster state into one record. This includes pending jobs, GPUs requested by pending jobs, GPUs in use on each node and the wait times of pending jobs. Rules will read from it later. For now, just log it.
- [ ] **Decision hook:** a function the controller calls at each check, e.g. `choose_policy(state, current_policy) -> policy`. For now it always returns the current policy, so nothing switches on its own.
- [ ] **Check interval:** how often the controller takes a snapshot and calls the hook, measured in simulated time. Make it configurable.
- [ ] **Test triggers:** a way to force a switch without rules, e.g. "switch to fairness at simulated time T" set in the run config. This is how the mechanism gets tested.
- [ ] **Switch log:** record every switch (simulated time, old policy, new policy and the reason, e.g. "scheduled test trigger") as part of the run output.
- [ ] Build it as an external script (D2b) that starts with each run and stops when the run ends. It reads state with `squeue`/`sinfo` and applies profiles with `scontrol`, timed by the simulator's clock.

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
| 1 | 0 + 1 | Decisions made; simulator running the 24-GPU cluster |
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
| The simulator's Slurm version is old or behaves differently from current Slurm docs | Check its version in Phase 1 and use the documentation for that version |
| GPU support in the simulator is limited | Test GPU scheduling first, in Phase 1. If it fails, fall back to treating each GPU as a CPU-only node |
| A policy can't be expressed with standard Slurm settings | Simplify the policy to what config allows. Write a plugin only for that one policy, as a last resort |
| Workloads too small to show differences between policies | Increase load until the queue stays non-empty, so policies actually have choices to make |
| Some policy settings can't change without restarting `slurmctld` | Find out in Phase 1, and leave those settings out of the policy profiles |
| The external script can't keep pace with simulated time, so switches land late | Test in Phase 1. Run the simulator slower if it has a speed setting, or use a longer check interval. Moving the controller into a `slurmctld` plugin is the last resort |
---

## Gaps in the Spec

These sections of [SPEC.md](SPEC.md) are still empty and would make the plan sharper:
- **Requirements:** for example, how many workloads, and how long each simulated run should be.
- **Success criteria:** for example, how much a policy must improve over the baseline.
- **Constraints:** the deadline, the team size and who owns which phase.
