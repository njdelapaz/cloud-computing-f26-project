# Progress

The tracker for `ver_0.2`. `SPEC.md` says what to build, `WORKFLOW.md` says how, and this file says where things stand. It records status, decisions and results only.

## Status
- **Done:**
  - P0 (spec): `SPEC.md` is the merged, approved spec, with the original brief kept as Appendix A.
  - P1 (simulator environment): the simulator builds from pinned sources, runs, and enforces memory; both do-nothing plugin forks match stock exactly.
  - P2 (interfaces): every config, data and log format is defined in SPEC §13, with a validator, an example and unit tests.
  - B1 (workloads): the trace filter, load-targeted sampler, simulator converter and episode finder are built and merged into `dev`.
  - A1 (ordering plugin, static): `priority/adaptive` takes its ordering weights from one policy; it matches stock exactly on the smoke trace and within 1 s on a contended trace (exact on both, and on the probe day, once the virtual clock came in). Merged into `dev`.
  - Run-to-run spread (option B, adopted): a simulator patch adds a virtual clock with fixed costs and fixes a stock bug that completed the wrong jobs. Every run now uses it. Identical runs are byte-identical on realistic days, and adaptive BASE matches stock exactly on the probe day (`scripts/check_vclock.sh`, 4 of 4; results/vclock/summary.md). The P1 and A1 gates still pass on it.
  - B2 (metrics): `analysis/metrics.py` computes every SPEC §13.4 metric from one run folder, plus the CI over seeds. It matches a hand-computed trace on all 51 columns (`scripts/check_b2.sh`; results/b2/summary.md). Ten metric details SPEC leaves open are proposed there, awaiting a decision, including a queue-dump format for churn and a fix for wait spread, which is infinite on real runs. Merged into `dev`; its sanity numbers on real runs predate the decay fix.
  - A2 (switching): `priority/adaptive` switches policies at runtime: override windows, `AUTO` with the `TMin` cooldown, `FIXED` and `FORCED`. A switch hot-swaps the ordering weights and publishes the policy to a shared state in slurmctld for A4. Every tick writes a decision-log row. The gate passes 6 of 6, and G1 still holds exactly on all three traces. A2 also fixed a simulator bug that kept the Age factor at 0 and stopped usage from decaying (the user's decision), and every earlier gate was rerun on the fixed image. Merged into `dev`.
- **Next:** A3 (states) and B3 (runner); they touch separate folders. Waiting for the user: whether A2's switch pass charges a simulated cost (SPEC §8; recommendation: no), B2's metric proposals (results/b2/summary.md), and the SPEC §11 #23 sensitivity update. A4 decides whether scoring charges a fixed cost.

## Phase tracker

| Phase | Status | Notes |
|---|---|---|
| P0 Spec | ✅ done | Gate passed: none of the items left in SPEC §11 blocks P1–P2 |
| P1 Simulator env | ✅ done | Gate passed: `scripts/check_p1.sh` passes 7 of 7 checks (results/p1/check_p1.txt) |
| P2 Interfaces | ✅ done | Gate passed: `scripts/check_p2.sh` validates all 8 config and example files and passes 30 unit tests (results/p2/check_p2.txt) |
| A1 Ordering plugin: static | ✅ done | Gate passed: `scripts/check_a1.sh` passes 5 of 5 gating checks (results/a1/check_a1.txt). Check 2 uses a contended trace instead of the probe day, because the simulator doesn't repeat itself on realistic load (results/a1/summary.md) |
| A2 Switching | ✅ done | Gate passed: `scripts/check_a2.sh` passes 6 of 6 gating checks (results/a2/check_a2.txt). On the A2 image, with the decay-time fix, `check_p1.sh` (7 of 7), `check_a1.sh` (5 of 5) and `check_vclock.sh` (4 of 4) still pass |
| A3 States | not started | |
| A4 Placement plugin | not started | |
| B1 Workloads | ✅ done | Gate passed: `scripts/check_b1.sh` (results/b1/check_b1.txt). 22,856,847 jobs kept; samples land within +2% of 0.85; episodes W1 55, W2 84 (volume floor added in review), W4 19, W6 182. Its spec follow-ups are applied |
| B2 Metrics | ✅ done | Gate passed: `scripts/check_b2.sh` (results/b2/check_b2.txt). Every column of the hand-computed trace matches (51 of 51), as do the `examples/` decision and placement logs; absent logs give empty values; 69 unit tests pass. Not gating: three real runs, cross-checked independently (results/b2/summary.md) |
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
| Controller edge cases (P2) | Eligible on the first tick; an override window takes over at once even during a cooldown; when it ends, back to `DefaultPolicy` with a fresh cooldown (SPEC §13.1) | Not specified before |
| Experiment modes (P2) | `FixedPolicy` for static runs and `ForceAlternate` + `ForcePeriod` for P5, both in `adaptive.conf` (SPEC §13.1) | Not specified before |
| `policies.conf` source (P2) | Generated by `scripts/make_policies.py` from the SPEC §6 mapping; P4 retunes the script's parameters, not the file | — |
| Sampling method (B1) | Seeded random keys taken in order until the load reaches 0.85, with a 2% overshoot skip (SPEC §9.2) | A fixed *p* per window, which landed within ±5% only about half the time |
| W2 rule (B1) | Volume floor added: memory-heavy core-hours at least their median per window; 91 → 84 episodes (SPEC §9.3) | The starting rule, which flagged 11 near-idle windows |
| Window and warm-up length | Both default to 1 day in B1's sampler; still to decide in B3/P4 | — |
| A1 gate check 2 | A 41-job contended trace, within 1 s, with the realistic day reported as spread but not gating. The user chose to test `ClockScaling = 0.001` first, then fall back to this | Identity on a realistic W0 trace, which the simulator can't repeat |
| `ClockScaling` | No longer used: the virtual clock ignores it. On the real clock it stays 1.0, because 0.001 cut the spread but didn't remove it, gave no speed-up, and stops backfill's time limits binding | — |
| Run-to-run spread in experiments | Adopted option B (the user's decision, after the trial passed its gate). Every run uses `ClockMode = virtual` with the fixed costs, so one run per seed is enough and seeds vary only the workload. Applied to SPEC G1, §8, §11 and §12, and to WORKFLOW | To decide before P4, among repeats per seed, more seeds, or patching the simulator. Then a trial of option B |
| Simulator patches (option B) | `docker/patches/sim_determinism.patch`, applied to the pristine clone at image build. It adds the virtual clock, which ver_0.2's templates turn on, and fixes stale completion events in both clock modes. Edit it with `scripts/sim_patch.sh` | Stock simulator, unpatched |
| Fixed simulated costs (option B) | Fitted from six real-clock runs on the fixed image, with least squares or means so the total time is kept: main pass 155 µs + 9.5 µs per job tested; backfill 421 µs per job tested; submit 997 µs; completion 130 µs; background call 291 µs; decay run 7.6 ms. slurmdbd calls are not charged (a real slurmctld runs them on other threads). In both `sim.conf.in` templates | — |
| Regression standard (G1) | Exact (start, end and node) on the smoke trace, the contended trace and a realistic day, on the virtual clock (SPEC G1, §11 #22; WORKFLOW's A1 and A4 gates) | "Identical on the same trace", including a realistic W0 trace → exact on smoke, within 1 s on contended, a realistic day only reported as spread (A1, before the virtual clock) |
| Metric details SPEC §13.4 leaves open (B2) | **Proposed, awaiting the user's decision.** For now `analysis/metrics.py` does this: linear-interpolation percentiles; log rows counted in [`warmup_end`, `window_end`); `score_us` over scored node choices only; level-3 cycle times taken from the cycle just before and just after each switch; pending meaning submit ≤ t < start, with the stranded median over all pending jobs; job ids in the logs being Slurm's JobId. Details are in results/b2/summary.md, proposals 3–10 | — |
| Queue-dump format for churn (B2) | **Proposed, awaiting the user's decision:** `log/queue_dumps.csv` with columns `sim_time,phase,rank,job_id,priority`, written by A2 at each switch that changes the ordering weights (results/b2/summary.md, proposal 3) | SPEC §4.2 mentions the dumps but gives no format |
| Fairness metrics on real runs (B2) | **Open.** Wait spread is ∞ on every real run because 6–12 users have zero mean wait. The recommendation is worst ÷ max(best, 10 s). The Jain index over per-user mean bounded slowdown is dominated by users with one or a few short jobs (0.61 → 0.13 from one job on 02-12). For both, SPEC is unchanged until the user decides (results/b2/summary.md, proposals 1–2) | — |
| Sharing the policy between plugins (A2) | A module compiled into slurmctld itself (`plugin/adaptive_policy/`, added to slurmctld's sources by `docker/register_plugins.sh`). slurmctld exports its symbols, so both plugins call it as they call `lock_slurmctld()`. The controller publishes; A4's scorer reads, and falls back to stock placement while nothing is published. The A4 half is still open | SPEC §4 left the hook to A2 and A4. Considered: a shared library both plugins link (needs a build order across plugin folders), or finding the priority plugin's symbols with `dlopen(RTLD_NOLOAD)` (depends on load order) |
| Windows in the experiment modes (A2) | A window wins in every mode. After it, the run goes back to its own policy: `DefaultPolicy` (AUTO), `FixedPolicy` (FIXED), or the first `ForceAlternate` policy, with the alternation restarting (FORCED). So the "Override only" run of SPEC §10 is `FixedPolicy=BASE` plus a schedule | SPEC §13.1 says only what happens in AUTO |
| Cooldown details (A2) | Counted in whole controller ticks: an interval within half a tick of `TMin` (or `ForcePeriod`) reaches it. A window's end starts a fresh cooldown even when nothing switched. A switch in AUTO needs a sample, so a failed sample keeps the policy. `scripts/validate.py` now checks the same rules | `validate.py` restarted the cooldown only at switches and compared seconds exactly, so it would have rejected a correct log with a window longer than `TMin` |
| Default schedule (A2) | `config/adaptive.conf` points at the empty `config/schedule_none.conf`. `config/schedule.conf` stays the W6 example, for runs that ask for it | It pointed at `config/schedule.conf`, which would have put a 12:00–17:00 → U window into every run, the static runs and the G1 checks included |
| `swap_us` and `pending_jobs` (A2) | `swap_us` is the work done under the job write lock: publish, set the weights, and the recompute pass when the ordering weights differ. It excludes the wait for the lock. `pending_jobs` is counted on every tick by walking the job list, because the log needs it. That walk counts as logging, not as a state sample for G4 | — |
| Frozen decay time in the simulator (A2) | Fixed (the user's decision). The simulator path of `_decay_thread()` now sets `start_time = time(NULL)` on every call: in stock multifactor through a new patch, `docker/patches/sim_decay_time.patch`, and in the fork directly. Age now counts, usage decays, and running jobs accrue usage each tick. The fixed costs are kept: the decay's measured real time rose about 3% (median), and halving or doubling every cost moved mean wait about 3%. All gates were rerun. Recorded in SPEC (the user's decision): §11 #1 and §12 name both patches, and a new §11 #24 records the fix | Unpatched: the age factor was always 0 and usage never decayed, so the ten trigger policies ordered jobs in only two ways |
| Switch pass on the virtual clock (A2, SPEC §8) | **Recommended: don't charge it** (waiting for the user's decision; nothing in SPEC changed). Measured with F ↔ U forced on every tick, so every switch recomputes priorities. On the probe day: 6,001 switches, `swap_us` median 1 µs and max 196 µs, with up to 192 pending jobs (about 10 µs + 0.5 µs per pending job). All the switches together took 36 ms of real time, against 194 s of simulated scheduler time charged in the same run (0.019%; 0.032% on the two-day sample). The largest switch measured, 380 µs, is 5% of the `DecayCost` charged on the same tick. That is far inside the cost model's own error (halving or doubling every cost moved mean wait about 3%), and the real cost is still measured, as `swap_us` and in the cost log | Charge a fixed cost per switch through `sim_charge()`, fitted from `swap_us` like the other costs. That needs a new config key (a SPEC change) |
| Cost sensitivity after the decay fix (SPEC §11 #23) | **Proposed, awaiting the user's decision:** #23 says halving or doubling every cost moved mean wait about 3%. Since the decay fix it moves mean wait and p95 by up to 20% (halved +20%, doubled −2% and −4%), and the virtual run falls outside the real-clock range on mean, p95 and waits over 60 s. Proposed: update #23's figure and say the virtual clock matches the real clock on aggregates only loosely (results/vclock/summary.md) | — |

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

### P2: interfaces
- **Formats in SPEC §13:**
  - config grammars for `policies.conf`, `schedule.conf` and `adaptive.conf`
  - the neutral trace (`jobs.csv`), window metadata (`meta.json`) and episodes (`episodes.csv`)
  - the decision log and the placement log
  - exact metric definitions, including stranded core-hours, churn and the t-based confidence interval
- **Files:**
  - `config/policies.conf` holds all 11 policies, generated from the §6 mapping and matching the §6 table.
  - `config/schedule.conf` holds the daily peak window (12:00–17:00 UTC → U) and one one-off window.
  - `config/adaptive.conf` holds the defaults, with placeholder thresholds until P6.
  - `examples/` has one small example of each data and log format. The decision-log example walks through sampling, a cooldown, hysteresis and an override window.
- **Checks:**
  - `scripts/validate.py` is stdlib-only, with one validator per format. The decision-log validator also checks the G4 rule (no sampling unless eligible) and that `eligible` follows the cooldown.
  - `tests/test_validate.py` has 30 tests: every example passes, and each rule rejects a broken file.

### B1: workloads
- **Code:**
  - `workloads/convert/`: the filter (cached to parquet), the sampler and the simulator converter
  - `workloads/episodes/find_episodes.py`: the episode finder
  - `scripts/check_b1.sh`: the gate
  - `tests/test_workloads.py`: 17 tests
  - Full write-up: `results/b1/summary.md`.
- **Filter:** 22,856,847 jobs kept, from 649 users in 94 accounts. 1.661% had their runtime capped at the time limit.
- **Sampling:** all 12 gate windows land between 0.850 and 0.864 offered load, and all 640 sweep samples are within ±5%. The cost is that the very largest jobs are kept at about half the nominal rate.
- **Episodes:** W1 55, W2 84, W4 19 and W6 182. Prefer W1 floods with many jobs (`jobs=` in `detail`): 11 floods have under 300 jobs and shrink to a handful at about 3.3% sampling.
- **For B3/P4:**
  - Window and warm-up lengths are both 1 day by default.
  - The warm-up's load isn't controlled; it ranged from 0.18 to 1.67 in the gate windows.
  - Every submit lands 11 s after its trace time because of the simulator's start-up lag, so B2 should take submit times from the simulator's output.
  - Use strong weekdays for W6.

### A1: ordering plugin (static)
- **Plugin:** `priority/adaptive` reads `adaptive.conf` and the `policies.conf` it names (parser in `plugin/priority_adaptive/adaptive_conf.c`, 47 tests). The active policy is `FixedPolicy`, otherwise `DefaultPolicy`, and its weights replace `slurm.conf`'s. A malformed file is fatal at start-up. With no `adaptive.conf` the plugin keeps `slurm.conf`'s weights. Full write-up: `results/a1/summary.md`.
- **Run script:** `scripts/sim_run.sh` gained `ADAPTIVE=` and `ADAPTIVE_SET="Key=Value ..."`. A run given either must log `loaded policy`, so it can't quietly fall back to `slurm.conf`'s weights.
- **Gate** (`scripts/check_a1.sh`, 5 of 5):
  - exact match with stock on the smoke trace
  - within 1 s on a 41-job contended trace, with 0 node differences and 0 order flips; a JobSize-only control flips 333 pairs
  - `FixedPolicy=U` gives submission order
  - a malformed file is fatal
  - the parser tests pass
- **Run-to-run spread, the key finding for P4–P8:**
  - The simulated clock runs at real speed while slurmctld works, so a scheduling pass that crosses a second boundary shifts a start by 1 s. On realistic load the shift snowballs.
  - Two identical stock runs of the probe day differ on about 2,100 of 3,028 jobs, with mean waits of 1,099 s and 5,727 s.
  - With `ClockScaling = 0.001` they still differ on 969 jobs (mean waits 1,975 and 1,659 s), with no speed-up, and backfill's time limits would stop binding. `ClockScaling` stays 1.0.
  - On the contended smoke trace the jitter stays at 1 s and doesn't grow.
- **Image:** A1 built `ver02/slurm-sim:a1` from the merged `docker/Dockerfile`. Both it and `ver02/slurm-sim:p1` predate the simulator patch and can't read the new `sim.conf` keys. Every script now defaults to `ver02/slurm-sim:vclock`.

### Option B: deterministic simulator (adopted)
Full write-up: `results/vclock/summary.md`. Image: `ver02/slurm-sim:vclock`, the default for every script.
- **Two causes of the spread, not one:**
  - the real-speed clock A1 found;
  - a stock simulator bug. Completion events held a pointer to the job's record, which is freed when the job ends. Every job gets a second completion event, and when one fired after its job had ended, it could complete whichever newer job had reused that memory. Example: `jobid_1360` ended after 15 s instead of 2,601 s. The patch passes the job id instead and ignores events whose job has ended.
- **Patch:** `ClockMode = virtual` makes slurmctld's clock move only on time skips, main-thread sleeps and the fixed costs (decision rows above). slurmctld runs no other threads in the simulator, so with the clock and the bug both fixed, a run depends only on its inputs.
- **Gate** (`scripts/check_vclock.sh`, 4 of 4, all on the virtual clock):
  - two identical runs give a byte-identical `jobcomp.log` on the smoke trace, the contended trace, the probe day, and the 2024-01-30 seed 2 and 2024-02-12 seed 3 samples (6,092 and 10,947 jobs)
  - adaptive BASE matches stock exactly on the smoke trace, the contended trace and the probe day
  - the JobSize-only controls differ
  - the costs are set and charged
- **Fidelity, probe day:**
  - virtual clock: mean wait 847 s, p95 2,137 s
  - four real-clock runs on the fixed image: 831–902 s and 2,048–3,357 s
  - with the bug fixed, the real clock's aggregate spread shrinks from A1's 5× to about ±4% in mean wait. But any two real-clock runs still differ on 1,751–2,319 of the 3,028 jobs; virtual-clock runs differ on none
- **Cost sensitivity:** halving or doubling every cost changes 2,339 jobs but moves the mean wait by about 3%.
- **Speed:** about 700 s of real time per probe day on the virtual clock, against about 745 s on the real clock. Both are dominated by synchronous slurmdbd round trips (89% of real time in a diagnostic run). Those don't affect virtual-clock results, so a faster database setup could speed up runs; that's a candidate for B3.
- **Adopted** (the user's decision):
  - Both `sim.conf.in` templates set `ClockMode = virtual`.
  - SPEC edits: G1 and §11 #22 require exact matches on a realistic day; §8 covers timing on the virtual clock, with real cycle times taken from the cost log; §4.2 adds the cost log; §6.2 notes that scheduler cost is modeled; §10 runs each seed once; §11 #1 and the new #23 record the patch; §12 adds `docker/patches/`; the open spread item is closed.
  - WORKFLOW: the A1 and A4 gates are exact on the probe day; A2 and A4 decide whether to charge their own costs; the repeatability risk is retired and a fixed-cost risk added.
- **Earlier gates rerun on the new defaults:** P1 passes 7 of 7, with a 0 s spread across the three determinism runs (`results/p1/check_p1_vclock.txt`). A1 passes 5 of 5, with 0 exact-match differences on the contended trace (`results/a1/check_a1_vclock.txt`). The original `check_p1.txt` and `check_a1.txt` stay as they were when those phases passed.

### B2: metrics
Full write-up: `results/b2/summary.md`.
- **Code:** `analysis/metrics.py` is standard-library only.
  - One command per run: `analysis/metrics.py RUN_DIR --meta META.json`, which prints a JSON row; `--csv` appends the row to a table.
  - `aggregate` gives the mean ± 95% CI over seed rows (t = 2.776 for 5 seeds). B3 can call `run_metrics()` and `aggregate()` directly.
  - When a decision, placement, queue-dump or cost log is missing, its columns are empty instead of an error.
  - It takes about 0.7 s for a 6,000-job day.
- **Hand-computed trace:** `tests/data/b2/` holds a 7-job trace on the 3-node smoke cluster. Its `README.md` derives every value and `hand.json` holds them. The trace's files pass the SPEC §13 validators.
- **Gate** (`scripts/check_b2.sh`):
  - all 51 columns match the hand calculation;
  - the `examples/` decision log (4 switches, mean `swap_us` 799.5) and placement log (3 scored node choices, mean `score_us` 12.3) match;
  - a run with only `jobcomp.log` leaves all 24 overhead columns empty;
  - the unit tests pass: 69, of which 22 are new in `tests/test_metrics.py`.
- **Sanity check on real runs** (stock BASE on the virtual clock):

  | Run | Mean / p95 wait (s) | Core / memory utilization | Load SD | Stranded core-h | Jain |
  |---|---|---|---|---|---|
  | 01-30 seed 2 | 6,525 / 20,193 | 0.75 / 0.93 | 0.06 | 3,048 | 0.10 |
  | 02-12 seed 3 | 51 / 499 | 0.64 / 0.67 | 0.28 | 220 | 0.13 |
  | probe day | 847 / 2,136 | 0.52 / 0.63 | 0.25 | 891 | 0.04 |

  - A per-second numpy recomputation agrees to 9 digits on waits, utilization, load SD, max load and stranded core-hours.
  - The completion records match `jobs.csv` exactly.
- **Findings:**
  - Wait spread is infinite on every real run.
  - The Jain index is dominated by users with few short jobs.
  - 01-30 is memory-bound: 93% memory against 75% core utilization at 0.85 offered core load.
  - Makespan is set by the longest measured job (6.7 days on 01-30).
  - Ten metric details SPEC leaves open, including the queue-dump format for churn, are proposed in the summary and await a decision.

### A2: switching
Full write-up: `results/a2/summary.md`. Image: `ver02/slurm-sim:a2`, with both simulator patches; since the merge it is the default for every script.
- **Code:**
  - `plugin/priority_adaptive/adaptive_ctl.c`: the pure `decide(snapshot, config, now, controller_state)`
  - `_controller_tick()` in the decay loop: decides, hot-swaps, logs
  - `plugin/adaptive_policy/`: the shared policy state, compiled into slurmctld
  - `adaptive_conf.c`: now also parses `schedule.conf`
  - `AUTO` keeps `DefaultPolicy` until A3 adds its sampler and classifier
  - tests: `plugin/tests/test_decide.py` (26), `test_adaptive_policy.py` (2), and `test_adaptive_conf.py` with 13 more (60)
- **Configs:** `config/adaptive.conf` defaults to the empty `config/schedule_none.conf`; `config/smoke/schedule_g2.conf` holds the G2 test windows.
- **Gate** (`scripts/check_a2.sh`, 6 of 6, on the virtual clock):
  - unit tests pass
  - G1: adaptive BASE with no windows matches stock exactly on the smoke trace, the contended trace and the probe day (0 of 3,028 jobs differ), with one AUTO row per tick and no switches
  - G2, on the contended trace: a one-off window [00:20:30, 01:00:30) → U gives OVERRIDE from 00:21:00 and AUTO from 01:01:00, both 30 s after the boundary, which falls mid-tick. A daily 01:30–01:45 → F window switches exactly on the minute
  - the switch to U recomputed all 40 pending jobs in 9 µs. All 22 jobs that started inside the window started exactly as in a static `FixedPolicy=U` run
  - each window's end starts a fresh 10-min cooldown, with no sampling in it
  - `FIXED` and `FORCED` work in the plugin
- **Earlier gates on the A2 image, with the decay-time fix:** `check_p1.sh` 7 of 7, `check_a1.sh` 5 of 5 and `check_vclock.sh` 4 of 4 (`results/a2/check_{p1,a1,vclock}_on_a2.txt`). The committed `results/p1/`, `results/a1/` and `results/vclock/` outputs are unchanged. The gate numbers above are from the fixed image; before the fix the A2 gate also passed 6 of 6 (commit `87cec8d`).
- **Switch cost:** see the decisions row. The recommendation is not to charge it, and the user hasn't decided yet.
- **Simulator bug, fixed (the user's decision):** in the simulator the Age factor was always 0, and fair-share usage never decayed, in stock multifactor and the fork alike.
  - Cause: `_decay_thread()` set `start_time` only on its first call under `SLURM_SIMULATOR`. Fair Tree, the default, still recomputed every priority each tick, but against that frozen time.
  - Effect before the fix: every Age weight did nothing. On the contended trace `F`, `EQ` and `B>U>F` gave identical schedules.
  - Fix: `docker/patches/sim_decay_time.patch` for stock, and the same line in the fork. A job's age term now grows as `PriorityMaxAge` predicts. `B>U>F` now differs from `F` on 35 of 41 jobs (`F` and `EQ` still agree on that small trace).
  - New probe-day baseline (stock BASE): mean wait 815 s and p95 3,361 s, against 847 s and 2,137 s before.
  - Evidence: `results/a2/decay_probe.txt`; write-up in `results/a2/summary.md`.

### After merging A2 and B2
- **Image:** every script now defaults to `ver02/slurm-sim:a2`. Its patches and plugin sources match merged `dev` file for file.
- **Gates rerun on merged `dev`:** P1 7 of 7, A1 5 of 5, vclock 4 of 4, A2 6 of 6, B2 pass, with 72 Python unit tests (A2's 50 plus B2's 22). Every gating line matches A2's reruns on the same image (`results/a2/check_*_on_a2.txt`); only real-time figures differ. The committed result files are unchanged.
- **B2's metrics after the decay fix** (stock BASE, virtual clock):

  | Run | Mean / p95 wait (s) | Stranded core-h | Jain |
  |---|---|---|---|
  | 01-30 seed 2 | 5,711 / 25,430 | 3,072 | 0.10 |
  | 02-12 seed 3 | 56 / 490 | 256 | 0.13 |
  | probe day | 815 / 3,361 | 859 | 0.04 |

  `results/b2/` still holds the numbers from before the fix. Wait spread is still infinite on all three runs.
- **Fidelity after the decay fix:** the four real-clock probe-day runs and the cost-sensitivity runs were redone on the fixed image (`results/vclock/check_vclock_decay.txt`; the old runs are in the git-ignored `results/vclock/predecay/`). The gate still passes 4 of 4.
  - The virtual run now falls inside the real-clock range only on the median: mean wait 815 s against 688–790 s, p95 3,361 s against 3,895–4,316 s, jobs waiting over 60 s 982 against 1,052–1,084. Before the fix it was inside on mean, median and p95.
  - The costs didn't move: fits from the probe day's real-clock runs before and after the fix agree within 7% on every kind except backfill per job (294 → 234 µs; `results/vclock/costs_decay.txt`). The templates are kept.
  - Sensitivity grew: halving every cost now moves mean wait and p95 by +20% (about 3% before); doubling moves them −2% and −4%.
  - The cause isn't established. The leading candidate is the slurmdbd round trips, which move simulated time only on the real clock (about 420 s of a 714 s run); with Age live, that time changes priorities. Write-up: `results/vclock/summary.md`, "After the decay fix".

## Change log

| Date | Commit | Change |
|---|---|---|
| 2026-11-21 | `c686905` | Initial brief (`SPEC.md`) |
| 2026-11-23 | `c21ef57` | Draft spec, workflow and `CLAUDE.md` added |
| 2026-11-24 | `a28fe3a` | Workloads moved to the real trace; cluster made CPU-only; `.gitignore` for `dataset/` and `.venv/` |
| 2026-11-24 | `ec1037c` | Policies split into ordering (fairness) and placement (balance, utilization); sign-offs recorded |
| 2026-11-24 | `d40689a` | P0 finished: draft merged into `SPEC.md`, `SPEC.proposed.md` removed, `PROGRESS.md` created |
| 2026-11-24 | `bebf49e` | P1 finished: simulator image, smoke configs and checks, do-nothing plugin forks, speed probe |
| 2026-11-24 | `04add00` | P2 finished: interface formats in SPEC §13, config files, examples, validators and tests |
| 2026-11-24 | `b5998e9` | B1 finished: trace filter, sampler, converter, episode finder and gate (B1 session) |
| 2026-11-24 | `90eee24` | B1 merged into `dev` |
| 2026-11-24 | `8feb002` | B1's spec edits applied (SPEC §9.2, §9.3, §13.2); A1 repeatability finding recorded |
| 2026-11-24 | `e0b38e2` | A1: `priority/adaptive` takes ordering weights from `policies.conf` (A1 session) |
| 2026-11-24 | `bec7565` | A1 finished: contended-trace identity check, gate 5 of 5 (A1 session) |
| 2026-11-24 | `2d6b890` | A1: `ClockScaling = 0.001` tested on the probe day; runs still don't repeat (A1 session) |
| 2026-11-24 | `2db72b9` | A1 merged into `dev` |
| 2026-11-24 | `7d7bbe6` | Option B: simulator determinism patch (virtual clock, stale-completion fix), fitted costs, `scripts/check_vclock.sh` gate 4 of 4; adopted as the default, with SPEC (G1, §4.2, §6.2, §8, §10, §11, §12) and WORKFLOW edits that replace the interim "within 1 s" regression standard; P1 and A1 gates rerun and passing |
| 2026-11-24 | `9b5a548` | B2 finished: `analysis/metrics.py` (SPEC §10, §13.4 metrics and the CI over seeds), the hand-computed trace in `tests/data/b2/`, `tests/test_metrics.py`, and the `scripts/check_b2.sh` gate (all 51 columns match), with a sanity check on three real runs. Metric-definition gaps proposed, with SPEC unchanged (B2 session) |
| 2026-11-24 | `87cec8d` | A2: switching in `priority/adaptive` (controller tick, pure `decide()`, hot swap, decision log), a shared policy state compiled into slurmctld, `schedule.conf` parsing, `scripts/check_a2.sh` gate 6 of 6; `validate.py`'s cooldown check fixed; A1 and vclock gates rerun on the A2 image and passing (A2 session) |
| 2026-11-24 | `b56f5c4` | A2: the simulator's frozen decay time is fixed (`docker/patches/sim_decay_time.patch` for stock multifactor, and the same line in the fork; `scripts/sim_patch.sh` takes a patch name). P1, A1, vclock and A2 gates rerun on the fixed image and passing (A2 session) |
| 2026-11-24 | `3e02198` | SPEC §11 #1, new §11 #24 and §12 record the decay-time patch (the user's decision; A2 session) |
| 2026-11-24 | `2c36c33` | B2 merged into `dev` |
| 2026-11-24 | `6463937` | A2 merged into `dev` |
| 2026-11-24 | `e5fe61c` | Every script defaults to `ver02/slurm-sim:a2`; all five gates rerun on merged `dev` and passing |
| 2026-11-24 | — | Real-clock probe-day runs and cost-sensitivity runs redone on the fixed image: gate 4 of 4, costs unchanged, fidelity looser (`results/vclock/check_vclock_decay.txt`) |
