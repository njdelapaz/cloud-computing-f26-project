# Multi-Policy Slurm Scheduler: Project Progress

As of 2026-11-08 · Brian Benson

## Status at a glance

Phase 2 (metrics and baseline) is done, and Phase 3a (fixed policies) is next. One command regenerates the five starter workloads from their seeds, runs the FIFO plus backfill baseline on each and writes every metric to a CSV, in about 10 minutes.

- **Last finished:** workload generator, converter, metrics script and baseline results for all five starter workloads (1,738 jobs, all finished).
- **Next up:** the four policy files (utilization, performance, load balance, fairness), each checked against the baseline on its own metric.
- **Code:** branch `dev` of the project repo, Phase 2 in commit `fb7f50f`.

## Phase tracker

Three of six phases are done.

| Phase | Status | Milestone | Notes |
| --- | --- | --- | --- |
| 0. Decisions | Done | D1–D4 settled | D2b revised after Phase 1 testing |
| 1. Environment setup | Done | Simulator running the 24-GPU cluster | Committed in `6f5406d` |
| 2. Metrics and baseline | Done | Workload generator, starter workloads, metrics script, baseline results | Committed in `fb7f50f` |
| 3a. Fixed policies | Not started | Four policy files that each beat the baseline on their own metric | `SETUP=<policy> scripts/run_baseline.sh` already runs and measures any policy |
| 3b. Switching mechanism | In progress | State snapshot, decision hook and check interval in the simulator | Policy switcher, scheduled triggers and switch log done in Phase 1 |
| 4. Evaluation | Not started | Results matrix and charts |  |
| 5. Write-up | Not started | Report and presentation |  |

## Decisions log

All Phase 0 decisions are settled; one (D2b) was reversed after Phase 1 testing showed the original choice can't work.

| ID | Decision | Choice | Why |
| --- | --- | --- | --- |
| D2b | Where the switching controller runs | **Inside the simulator:** a `switch_policy` event in the workload file, added by a project patch. *Revised from: an external script using `squeue`/`scontrol`.* | The simulator answers no client commands during a run, and a full reconfigure crashes it. The event also lands at exactly the scheduled simulated time. |
| D4 | Workloads | Synthetic job mixes from a seeded generator, with real traces possible later | One common workload format means a real trace later needs only a converter. |
| D3 | Cluster layout | 24 GPUs on 3 identical nodes (`gpu1`–`gpu3`, 8 GPUs each) | Fragmentation becomes the main contention, which is what packing vs. spreading policies differ on. |
| D2 | What "change policies" means | Policy switches during a run based on cluster state; switching rules come later | Fixed policies stay as the comparison points in Phase 4. |
| D1 | How a policy is implemented | A file of standard Slurm settings per policy, no custom scheduling plugin | Fastest to build. Phase 1 confirmed priority weights, node weight and LLN can change mid-run. |

## Phase 2 results

`scripts/run_baseline.sh` regenerates the starter workloads, runs the baseline on each and writes [results/baseline.csv](results/baseline.csv). Usage is in [sim/README.md](sim/README.md#workloads-and-metrics).

**Built:**

- **Workload generator** (`scripts/generate_workload.py`): turns a JSON spec in `sim/workloads/specs/` into a workload. A spec sets the seed, a target offered load, optional bursts, GPU-count and runtime distributions, time-limit accuracy, the timeout share, users and phases. Generating twice gives identical files.
- **Converter** (`scripts/convert_workload.py`): converts the common CSV format into the simulator's events format, and back. Converting the hand-written 20-job workload gives a byte-identical file. `--switch SECONDS:POLICY` adds scheduled switches to any workload.
- **Metrics script** (`scripts/metrics.py`): computes utilization, wait and turnaround, bounded slowdown, makespan, node load balance and Jain's fairness index for one run. It writes `metrics.json` and adds a row to a summary CSV. Utilization was cross-checked by hand on the 20-job run (0.787 both ways).
- **Starter workloads** (`sim/workloads/`):

| Workload | Jobs | Submissions | Offered load | Purpose |
| --- | --- | --- | --- | --- |
| `light` | 99 | 12 h | 0.36 | Sanity check: queue mostly empty |
| `heavy_small` | 665 | 12 h | 1.09 | 1–4 GPU jobs: fragmentation |
| `heavy_mixed` | 144 | 12 h | 1.19 | Up to 24 GPUs: contention for whole nodes |
| `shifting` | 511 | 18 h | 0.86 | 6 h of short 1–2 GPU jobs, then 12 h of long 4–24 GPU jobs |
| `skewed_users` | 319 | 12 h | 0.91 | `user1` submits 60% of jobs |

**Baseline (FIFO plus backfill):**

| Workload | GPU utilization | Mean wait | p95 wait | Mean bounded slowdown | Node GPU variance | Jain (slowdown) | Makespan |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `light` | 0.357 | 1 min | 7 min | 1.20 | 6.32 | 0.956 | 12.1 h |
| `heavy_small` | 0.896 | 46 min | 1.5 h | 6.40 | 0.51 | 0.991 | 14.6 h |
| `heavy_mixed` | 0.855 | 73 min | 4.7 h | 5.80 | 2.44 | 0.972 | 16.8 h |
| `shifting` | 0.819 | 7 min | 19 min | 1.80 | 3.60 | 0.998 | 18.9 h |
| `skewed_users` | 0.821 | 15 min | 49 min | 2.73 | 3.17 | 0.972 | 13.3 h |

Node GPU variance is the time-averaged variance of GPUs in use across the three nodes; 0 means perfectly even.

**Findings that affect later phases:**

- **Runs are faster than measured in Phase 1:** 290–774x real time, so all five baseline runs take about 10 minutes together.
- **Runs are nearly but not fully deterministic.** Re-running `light` with the same `dtstart` changed one job's start by 1 second. Policy differences must be well above that, and Phase 4 should repeat runs with different `dtstart` values.
- **The baseline already packs.** With equal node weights Slurm fills `gpu1` first. On `light`, `gpu1` did 45 GPU-hours and `gpu3` only 20. That gives the load-balance policy room to improve at light load. Under heavy load all three nodes stay full, so balance is near even anyway (variance 0.51 on `heavy_small`).
- **The fairness metric needs a decision.** On `skewed_users`, FIFO already gives a Jain index of 0.972 over per-user slowdown, because it treats every job alike. A fair-share policy deliberately makes the heavy user wait longer, which can *lower* that index. Jain over per-user wait is also unreliable at light load, where almost every wait is 0 (0.547 on `light`).
- **Per-node GPU use is reconstructed, not recorded.** `slurm_acct.out` doesn't give GPUs per node, so the metrics script replays job starts and ends. It is exact for single-node jobs and for multi-node jobs using whole nodes, which is all the starter workloads contain.

## Risks and open issues

The biggest open items are the spec's success targets and how fairness is measured; without both, "beats the baseline" stays undefined for the fairness policy.

| Risk or issue | Mitigation |
| --- | --- |
| A policy needs a setting the switch can't change (e.g. backfill parameters) | Simplify the policy to priority weights, node weight and LLN, or extend the switch code for one more setting |
| `shifting` is lighter than intended (offered load 0.86, mean wait 7 min), so its queue often drains | Raise the load of its second phase if switching shows no effect on it |
| Run-to-run noise could hide small policy differences | Repeat runs with different `dtstart` values in Phase 4 |
| The project patch changes simulator behavior beyond the switch | It only adds an event type. Re-run the bundled tutorial after any patch change |
| Rule-driven switching (Phase 3b) means C code inside the simulator, not a script | Keep the decision hook small; rules can be added later without touching the rest |
| A run never ends if every job is rejected | The run script stops it after a wall-clock limit and reports no jobs |

**Open questions:**

- [ ] How much must a policy improve on the baseline to count as a success?
- [ ] Which fairness measure counts: equal slowdown per user (Jain over slowdown), or each user's GPU-hours matching their fair-share target?
- [ ] Who owns each phase?
- [x] ~~How many workloads, and how long should each simulated run be?~~ Five starter workloads with 12 h of submissions each (18 h for `shifting`), 99–665 jobs.

## Next steps (Phase 3a)

Phase 3a is done when each of the four policy files beats the baseline on its own metric.

- [ ] Write `sim/etc/policies/utilization.conf`: pack jobs with node weights 1, 2, 3 so whole nodes stay free
- [ ] Write `sim/etc/policies/performance.conf`: favor small and short jobs through job-size and age weights
- [ ] Write `sim/etc/policies/load_balance.conf`: spread jobs with partition `LLN=YES`
- [ ] Write `sim/etc/policies/fairness.conf`: weight priority mostly on fair-share
- [ ] Run each with `SETUP=<policy> scripts/run_baseline.sh` and compare its CSV with `results/baseline.csv`

## Change log

Add a row at the top for each commit or milestone.

| Date | Commit | What changed |
| --- | --- | --- |
| 2026-11-08 | `fb7f50f` | Phase 2 done: workload generator, converter, metrics script, five starter workloads, baseline results |
| 2026-11-07 | `6f5406d` | Phase 1 done: 24-GPU simulation, run script, `switch_policy` patch, test workloads, docs |
| 2026-11-07 | — | D2b revised to in-simulator switching after testing showed no client commands work mid-run |
| 2026-11-07 | `edddbed` | Spec and development plan added; all Phase 0 decisions settled |
| 2026-11-07 | `ad5ad33` | Repository created |
