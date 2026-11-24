# Progress

The tracker for `ver_0.2`. `SPEC.md` says what to build, `WORKFLOW.md` says how, and this file says where things stand. It records status, decisions and results only.

## Status
- **Done:**
  - P0 (spec): `SPEC.md` is the merged, approved spec, with the original brief kept as Appendix A.
  - P1 (simulator environment): the simulator builds from pinned sources, runs, and enforces memory; both do-nothing plugin forks match stock exactly.
- **Next:** P2 (interfaces).

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| P0 Spec | ✅ done | Gate passed: none of the items left in SPEC §11 blocks P1–P2 |
| P1 Simulator env | ✅ done | Gate passed: `scripts/check_p1.sh` passes 7 of 7 checks (results/p1/check_p1.txt) |
| P2 Interfaces | not started | |
| A1 Ordering plugin: static | not started | |
| A2 Switching | not started | |
| A3 States | not started | |
| A4 Placement plugin | not started | |
| B1 Workloads | not started | |
| B2 Metrics | not started | |
| B3 Runner | not started | |
| P4 Static policies | not started | |
| P5 Switch overhead | not started | |
| P6 State calibration | not started | |
| P7 T_min | not started | |
| P8 Evaluation | not started | |
| Stretch: reconfigure baseline | not started | |
| P9 Report | not started | |

## Decisions log
The current decisions are listed in SPEC §11 (#1–19). This log keeps the history behind them, including choices that were reversed.

| Topic | Where it ended up | Earlier versions |
|---|---|---|
| What policies control | Fairness sets job order (`priority/adaptive`); balance and utilization set node placement (`select/adaptive`) (SPEC §11 #17–19) | All three policies were priority weights only |
| Meaning of balance | Even load across nodes, via placement (#2) | Even load across job sizes → CPU vs GPU nodes → cores vs GPUs on GPU nodes → cores vs memory as a priority factor |
| Cluster | 16 CPU nodes × 32 cores × 128 GB, one partition, CPU jobs only (#4, #5) | 64 CPU + 8 GPU nodes → 16 CPU + 3 GPU nodes (24 GPUs) → GPU nodes dropped |
| Workloads | Real Jan–Jun 2024 trace in `dataset/`, scaled by random job sampling; trigger workloads from real episodes (#10–12) | Synthetic generator |
| Users and shares | Users and accounts from the trace, equal shares per account (#5) | 40 synthetic users in 8 accounts |
| Calibration vs evaluation | January–February vs March–June (#15) | January vs February–March, when only three months were available |
| Spec sign-offs | Non-goals, weight totals and episode thresholds accepted (#13, #14, #16) | — |
| Simulator install (P1) | Lean image built here from pinned sources (`docker/Dockerfile`) | The author's prebuilt 5.8 GB image was the alternative |
| Placement plugin name (P1) | `select/cons_tres_adaptive` (SPEC §11 #18) | `select/adaptive` |

## Spec changes from P1 (approved and applied to SPEC.md)
Along with these, SPEC §8's timing note and the §11 window-length item now state what P1 measured.
1. **Rename the placement plugin to `select/cons_tres_adaptive`.** slurmctld only turns on its cons_tres code paths when `SelectType` contains "cons_tres", so a plugin named `select/adaptive` would quietly behave differently from stock and fail the G1 check. Affects SPEC G1, §4, §6.1, §11 #18, §12 and WORKFLOW A4.
2. **Settle two §11 items:**
   - The Slurm release is 23.11.0-0rc1, per the simulator's `META` file. The branch is named `slurm-23-02-sim` and the upstream README says 23.02, but the code itself is 23.11.
   - Memory is tracked as a consumable resource: confirmed.
3. **Fix §9.2 on runtimes.** The spec says `elapsed` is never above `timelimit`. In fact 1.66% of kept jobs ran past their limit, by a median of 16 s (the kill grace period); almost all of them are `timeout` jobs. The converter should cap runtime at the time limit.

## Results
Trace profiling, done with one-off scripts that aren't saved in the repo. B1's gate re-checks these numbers with the real filter and sampler.
- **Trace:** 23.5M jobs; about 22.9M remain after filtering, all single-node, from about 649 users in 94 accounts (SPEC §9.2).
- **Source cluster:** about 13,100 busy cores on average, about 25× the simulated cluster, so sampling keeps roughly 3.3% of jobs.
- **Job shapes:**
  - median job is 1 core
  - 9% of jobs ask for more than 8 GB per core
  - time limits are a median 11× the actual runtime
- **Episodes found with the starting thresholds:**
  - 58 user-flood windows from 22 users (W1)
  - 91 memory-heavy bursts (W2)
  - a daily peak at 12:00–17:00 UTC, about 1.6× the average rate (W6)
  - W3 and W5 need `BASE` simulation runs (P6)

### P1: simulator environment
- **Sources, pinned in `third_party/` (git-ignored):**
  - `ubccr-slurm-simulator/slurm_sim_tools` @ `21ba58d` (v3.0-branch, 2024-05-30)
  - `ubccr-slurm-simulator/slurm_simulator` @ `f99a71e` (slurm-23-02-sim, 2024-02-09)
  - Base image `ubuntu:22.04@sha256:b000e69a50ff940390233025e7a337fb8469b509e2334fa4d018ac30f3e18cdb`
  - The built image `ver02/slurm-sim:p1` is 3.1 GB, including the Slurm build tree the plugin forks need.
- **Gate checks** (`scripts/check_p1.sh`, 3 nodes × 4 cores × 16 GB, hand-written traces in `config/smoke/`): all 7 pass.
  - Age weights give submission order; job-size weights give largest first, exactly as predicted.
  - Three repeat runs are identical, with a 0 s spread.
  - With 12 GB jobs on 16 GB nodes, only three run at once and the fourth waits 120 s. Memory is enforced.
  - The do-nothing forks `priority/adaptive` and `select/cons_tres_adaptive` give results identical to stock under both weight sets.
  - The clock probe in the priority fork shows 1,018 s simulated over 29.8 s of real `CLOCK_MONOTONIC` time.
- **Clocks:**
  - The simulator links every binary and plugin with `-Wl,-wrap` for `time`, `gettimeofday`, `sleep`, `usleep` and `nanosleep`, so those return simulated time.
  - `clock_gettime()` isn't wrapped, so `CLOCK_MONOTONIC` is real time; `swap_us` and `score_us` should use it.
  - Backfill cycles advance the simulated clock by their real elapsed time (× `ClockScaling`). The scheduler's real CPU cost therefore counts in simulated time, and a heavily loaded host can change results. Runs need spare cores and shouldn't share the machine with heavy jobs.
- **Plugin constraints** (details in `plugin/README.md`):
  - The select fork's name must contain `cons_tres`, and it needs its own `plugin_id` (150), because all select plugins load at start-up.
  - The priority fork must keep the names `decay_handler_thread` and `_decay_thread`, which the simulator matches.
  - Registering the forks needs `autoreconf`, with stub GLib/GTK macros (`docker/m4/`).
- **Speed** (one real day on the 16-node project cluster, `scripts/p1_speed_probe.py`, 2024-01-15, seed 1):
  - 3,028 jobs (3.8% of that day) from 54 users in 36 accounts.
  - The day plus its drain is 4.2 simulated days and took 11.3 min of real time, about 530×.
  - Median wait 2 s, p90 77 min.
  - Average busy cores over the day were 258 of 512, because the cluster started empty with no warm-up.
  - At roughly 11 min per simulated day of submissions, a 1-day-window run matrix of about 600 runs is about 110 h serial. Window length and parallelism still need deciding (B3, P4).

## Change log

| Date | Commit | Change |
|---|---|---|
| 2026-11-21 | `c686905` | Initial brief (`SPEC.md`) |
| 2026-11-23 | `c21ef57` | Draft spec, workflow and `CLAUDE.md` added |
| 2026-11-24 | `a28fe3a` | Workloads moved to the real trace; cluster made CPU-only; `.gitignore` for `dataset/` and `.venv/` |
| 2026-11-24 | `ec1037c` | Policies split into ordering (fairness) and placement (balance, utilization); sign-offs recorded |
| 2026-11-24 | `d40689a` | P0 finished: draft merged into `SPEC.md`, `SPEC.proposed.md` removed, `PROGRESS.md` created |
| 2026-11-24 | — | P1 finished: simulator image, smoke configs and checks, do-nothing plugin forks, speed probe |
