# Progress

The tracker for `ver_0.2`. `SPEC.md` says what to build, `WORKFLOW.md` says how, and this file says where things stand. It records status, decisions and results only.

## Status
- **Done:** P0 (spec). `SPEC.md` is the merged, approved spec, with the original brief kept as Appendix A.
- **Next:** P1 (simulator environment). It starts by checking whether the UB simulator tracks memory as a consumable resource, since both placement terms and the utilization signal depend on it.

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| P0 Spec | ✅ done | Gate passed: none of the items left in SPEC §11 blocks P1–P2 |
| P1 Simulator env | not started | |
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

## Change log

| Date | Commit | Change |
|---|---|---|
| 2026-11-21 | `c686905` | Initial brief (`SPEC.md`) |
| 2026-11-23 | `c21ef57` | Draft spec, workflow and `CLAUDE.md` added |
| 2026-11-24 | `a28fe3a` | Workloads moved to the real trace; cluster made CPU-only; `.gitignore` for `dataset/` and `.venv/` |
| 2026-11-24 | `ec1037c` | Policies split into ordering (fairness) and placement (balance, utilization); sign-offs recorded |
| 2026-11-24 | — | P0 finished: draft merged into `SPEC.md`, `SPEC.proposed.md` removed, `PROGRESS.md` created |
