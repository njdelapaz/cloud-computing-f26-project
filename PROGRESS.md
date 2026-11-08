# Multi-Policy Slurm Scheduler: Project Progress

As of 2026-11-07 · Brian Benson

Snapshot of the live progress doc: https://claude.ai/artifact/3Z54MniFxPbAzZZEuba8kq. Edit the live doc and re-export rather than editing this file.

## Status at a glance

Phase 1 (environment setup) is done, and Phase 2 (metrics and baseline) is next. The 24-GPU cluster runs in the UB Slurm Simulator about 200x faster than real time, and the scheduling policy can now be switched at a set simulated time during a run.

- **Last finished:** mid-run policy switching through a new `switch_policy` simulator event, with node weight, least-loaded-node placement (LLN) and priority weights all verified by test runs.
- **Next up:** the workload generator, the converter into the simulator's format, the metrics script and the baseline runs.
- **Code:** branch `dev` of the project repo, latest commit `6f5406d`.

## Phase tracker

Two of six phases are done.

| Phase | Status | Milestone | Notes |
| --- | --- | --- | --- |
| 0. Decisions | Done | D1–D4 settled | D2b revised after Phase 1 testing |
| 1. Environment setup | Done | Simulator running the 24-GPU cluster | Committed in `6f5406d` |
| 2. Metrics and baseline | Not started | Workload generator, starter workloads, metrics script, baseline results | Users and accounts for fair-share already set up |
| 3a. Fixed policies | Not started | Four policy files that each beat the baseline on their own metric | Running a single fixed policy already works (`SIM_START_POLICY`) |
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

## Phase 1 results

One command now runs a workload on the simulated cluster: the 20-job test covers 1 h 49 min of simulated time in 33 s.

**Built:**

- **Cluster config** (`sim/etc/`): 3 nodes × 8 GPUs, 64 cores and 512 GB each, so 8 CPUs and 64 GB per GPU. Five users in three accounts for fair-share.
- **Run script** (`scripts/run_sim.sh`): runs one workload in Docker, stops it after a wall-clock limit, and fails on a fatal error or empty results.
- **Simulator patch and image** (`sim/patches/switch_policy.patch`, `docker/Dockerfile`): adds the `switch_policy` event and writes `policy_switches.csv` for each run.
- **Test workloads** (`sim/workloads/`): a 20-job size mix and a switching test.

**Switching test** — every switch landed at exactly its scheduled simulated time:

| Setting switched | Expected | Result |
| --- | --- | --- |
| Node weight | New jobs go to the lowest-weight node | Jobs moved to `gpu3` |
| Least-loaded node (LLN) | New jobs go to the idle node | Jobs went to `gpu2` |
| Priority weights (small jobs first) | A queued 1-GPU job overtakes an earlier 24-GPU job | It did |

**Findings that affect later phases:**

- The simulator runs Slurm **23.11**, not the 23.02 its docs state, so use the 23.11 docs.
- Request GPUs as `-N <nodes> --gpus=<G>`. The simulator rejects `--gpus-per-task`, and without `-N` a small job can split across nodes.
- A walltime of `-1` runs a job until its time limit; `0` ends it instantly.
- Job records don't list GPUs per job, so the metrics script must take GPU counts from the workload file.
- Settings other than priority weights, node weight and LLN (e.g. backfill parameters) can't be switched mid-run.

## Risks and open issues

The biggest open item is the spec: without success targets, "beats the baseline" stays undefined.

| Risk or issue | Mitigation |
| --- | --- |
| A policy needs a setting the switch can't change (e.g. backfill parameters) | Simplify the policy to priority weights, node weight and LLN, or extend the switch code for one more setting |
| Workloads too light to show differences between policies | Raise load until the queue stays non-empty |
| The project patch changes simulator behavior beyond the switch | It only adds an event type. Re-run the bundled tutorial after any patch change |
| Rule-driven switching (Phase 3b) means C code inside the simulator, not a script | Keep the decision hook small; rules can be added later without touching the rest |
| A run never ends if every job is rejected | The run script stops it after a wall-clock limit and reports no jobs |

**Open questions:**

- [ ] How much must a policy improve on the baseline to count as a success?
- [ ] Who owns each phase?
- [ ] How many workloads, and how long should each simulated run be?

## Next steps (Phase 2)

Phase 2 is done when the starter workloads regenerate from their seeds and baseline metrics come from a script.

- [ ] Write the converter from the common workload format into the simulator's events format, and test it on the 20-job workload
- [ ] Write the seeded workload generator (arrival rate, GPU-count and runtime distributions, users, phases)
- [ ] Generate the five starter workloads: light load, heavy small jobs, heavy mixed sizes, shifting mix, skewed users
- [ ] Write the metrics script: utilization, wait and turnaround times, load balance across nodes, Jain's fairness index
- [ ] Run the baseline (FIFO plus backfill) on every starter workload and save the results as CSV
- [x] Set up users and accounts for fair-share (done in Phase 1)

## Change log

Add a row at the top for each commit or milestone.

| Date | Commit | What changed |
| --- | --- | --- |
| 2026-11-07 | `6f5406d` | Phase 1 done: 24-GPU simulation, run script, `switch_policy` patch, test workloads, docs |
| 2026-11-07 | — | D2b revised to in-simulator switching after testing showed no client commands work mid-run |
| 2026-11-07 | `edddbed` | Spec and development plan added; all Phase 0 decisions settled |
| 2026-11-07 | `ad5ad33` | Repository created |
