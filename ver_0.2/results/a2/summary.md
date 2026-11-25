# A2: switching

`priority/adaptive` now switches policies at runtime: override windows from `schedule.conf`, `AUTO` with the `TMin` cooldown, and the two experiment modes `FIXED` and `FORCED`. It hot-swaps the ordering weights, publishes the active policy to a shared state that A4's placement scorer will read, and writes the decision log (SPEC §13.3). Gate: `scripts/check_a2.sh` (output in `check_a2.txt`). Image `ver02/slurm-sim:a2`; host AMD Ryzen 9 3950X. Every run is on the virtual clock.

## What was built

| File | What it does |
|---|---|
| `plugin/adaptive_policy/` | the shared policy state: `adaptive_policy_publish()`, `_get()`, `_seq()`, `_clear()`, guarded by a mutex. Compiled into slurmctld itself, so the process has one copy |
| `docker/register_plugins.sh` | copies that folder to `src/slurmctld/adaptive_policy/` and adds it to slurmctld's sources |
| `plugin/priority_adaptive/adaptive_ctl.{c,h}` | `adaptive_decide()`, the pure `decide(snapshot, config, now, controller_state)`, plus the decision-log row formatter |
| `plugin/priority_adaptive/adaptive_conf.{c,h}` | the parser now reads `schedule.conf` too (one-off and daily windows, overlap checks), and `ForceAlternate` makes the run start on its first policy |
| `plugin/priority_adaptive/priority_adaptive.c` | `_controller_tick()` in the decay loop, the hot swap, the decision log, and loading and reconfiguring the controller |
| `plugin/tests/test_decide.py`, `adaptive_ctl_cli.c` | the controller's unit-test table: scripted ticks through the real parser and `adaptive_decide()`, every log checked by `scripts/validate.py` |
| `plugin/tests/test_adaptive_policy.py`, `adaptive_policy_test.c` | the shared state, set up as in slurmctld: two `dlopen`'d plugins share one copy, concurrent readers never see a torn one |
| `plugin/tests/test_adaptive_conf.py` | the A1 parser tests, plus 13 for `schedule.conf` |
| `scripts/validate.py`, `tests/test_validate.py` | the decision-log check now restarts the cooldown at a window's end and counts it in whole ticks (below) |
| `config/schedule_none.conf` | an empty schedule, now the default in `config/adaptive.conf` |
| `config/smoke/schedule_g2.conf` | the G2 test windows |
| `scripts/check_a2.sh`, `check_a2.py` | the gate |

## How it works

**The tick.** The simulator calls the decay loop once per `PriorityCalcPeriod` (60 s). `_controller_tick()` runs inside it, just before the Fair Tree recalculation:
1. `adaptive_decide()` gives the mode, whether the tick is eligible, whether it sampled, and the policy in force afterwards. It runs under the controller's own mutex and never holds it while taking slurmctld's locks.
2. On a switch, under the job write lock: publish the new policy to the shared state, set its ordering weights, and if they differ from the old policy's, recompute every pending job's priority in one pass. `swap_us` is this work's `CLOCK_MONOTONIC` time; the wait for the lock isn't included. Without a switch, the tick only counts pending jobs under the job read lock.
3. Write the decision-log row. A switch is also logged to `slurmctld.log`.

**The rules** (`adaptive_ctl.h`; SPEC §8, §13.1):
- A window covering the tick gives `OVERRIDE` with the window's policy, at once, in any mode. Nothing is sampled.
- The first tick after a window goes straight back to the run's own policy: `DefaultPolicy` in `AUTO`, `FixedPolicy` in `FIXED`, the first `ForceAlternate` policy in `FORCED`. It doesn't sample, and a fresh cooldown starts, even if nothing switched.
- `AUTO` is eligible on its first tick and once `TMin` has passed since the last switch or window end. Only then is the sampler called. A switch in `AUTO` needs a sample, so a tick whose sampler fails keeps the policy.
- A2 has no sampler yet: `AUTO` stays on `DefaultPolicy` until A3 passes its snapshot function and classifier to `adaptive_decide()`.
- `FIXED` stays on `FixedPolicy`. `FORCED` starts on the first policy and switches to the other every `ForcePeriod`. Neither is ever eligible or samples.
- Intervals are counted in whole ticks: one within half a tick of `TMin` or `ForcePeriod` counts as reaching it. Ticks fall on whole simulated minutes (the first lands at +1 s), but a busy event loop could make one land a second late, and that shouldn't push a switch back a whole tick.

**Sharing the policy.** SPEC §4 asks for a small module compiled into slurmctld. slurmctld is linked with `-export-dynamic`, so plugins already call it directly (`lock_slurmctld()`, `job_list`), and they reach the new module the same way. One copy exists whichever plugins load and in whatever order. Until something is published, `adaptive_policy_get()` returns -1, which tells the scorer to use stock placement. A4 needs only an include path and the call.

## Gate

All 6 gating checks pass; see `check_a2.txt`.

| Check | Result |
|---|---|
| 1 Unit tests | decide() table 26, parser 60, shared state 2, `tests/` 50 (11 skipped as before): all pass |
| 2 G1: adaptive BASE, no windows == stock | 0 differences in start, end or nodes on the smoke trace (7 jobs), the contended trace (41) and the probe day (3,028). Each decision log is valid, has one row per tick (18, 121 and 6,002), is AUTO throughout, and never switches |
| 3 G2: windows switch within one tick | one-off window [00:20:30, 01:00:30) → U: OVERRIDE from 00:21:00 and AUTO from 01:01:00, both 30 s after the boundary, which falls mid-tick. Daily window 01:30–01:45 → F: OVERRIDE from 01:30:00, AUTO from 01:45:00. Switches at all four boundaries |
| 4 Hot swap works | the switch to U recomputed all 40 pending jobs (9 µs). All 22 jobs that started inside the window started at the same time, on the same nodes, as in the static `FixedPolicy=U` run. None of the 18 that started after it did, and 40 of 41 jobs differ from adaptive BASE |
| 5 Fresh cooldown after a window | after each window: 10 ticks not eligible, then eligible (`TMin = 10min`); 0 ticks sampled while not eligible |
| 6 FIXED and FORCED in the plugin | `FixedPolicy=U`: 123 FIXED ticks, 0 switches. `ForceAlternate=F,U` every tick: 125 FORCED ticks, 124 switches, each recomputing priorities |

The earlier gates on the new image: `scripts/check_a1.sh` passes 5 of 5 (`check_a1_on_a2.txt`), and `scripts/check_vclock.sh` passes 4 of 4: byte-identical repeats on all five traces, adaptive BASE == stock, and the probe day's mean wait is still 847 s (`check_vclock_on_a2.txt`).

The unit-test table covers the three groups WORKFLOW asks for: override boundaries (7 tests: on and off the tick grid, start inclusive and end exclusive, daily windows on two days, a window ending 23:59, adjacent windows, a window whose policy is already active), a cooldown with no sampling (7), and override vs cooldown (3: a window inside a cooldown, a window longer than `TMin`, a window on the first tick). The rest cover `FIXED` and `FORCED` with and without windows, the recompute flag, and the A2 stub.

## Swap cost and the `sim_charge()` decision

SPEC §8 leaves it to A2 to decide whether the switch pass charges a fixed simulated cost through `sim_charge()`. On the virtual clock, real CPU time doesn't move simulated time unless something charges it. Measured with forced F ↔ U alternation on every tick, so every switch recomputes priorities. This is the most switching any configuration can do, since `TMin` ≥ 1 tick.

| Run | Switches | `swap_us` median / p95 / max | Pending jobs median / max | Fit (switches with pending > 0) |
|---|---|---|---|---|
| contended trace | 124 | 5 / 14 / 20 | 19 / 40 | 2.2 + 0.19 µs × pending |
| probe day | 6,001 | 1 / 25 / 173 | 0 / 209 | 4.7 + 0.37 µs × pending |
| 2024-02-12 seed 3 sample (10,947 jobs) | 6,864 | 1 / 60 / 173 | 0 / 157 | 15.9 + 0.42 µs × pending |

- **Per switch:** mean 4.8 µs on the probe day, or 41 µs when 50 or more jobs are pending, which is about 0.45 µs per pending job.
- **Per run:** on the probe day, all 6,001 switches together took 28.5 ms of real time. The same run charged 197 s of simulated scheduler time: background calls 104.8 s, decay runs 45.6 s, backfill 40.2 s, main passes 3.5 s, submissions 3.0 s and completions 0.4 s. So charging every switch in full would add 0.014% to the charged total. On the two-day sample it would add 0.031% (59 ms against 194 s).
- **Per tick:** the largest switch (173 µs) is 2.3% of the `DecayCost` (7,592 µs) already charged on the same tick, and the mean switch is 0.06% of it.
- **The tick itself:** the decay event's real time averaged 9,031 µs for stock, 9,089 µs for adaptive with no switches, and 9,094 µs for adaptive switching every tick (log/cost.csv). That is 0.6% more, within run-to-run noise, and the host was running other gates at the time.
- **Context:** Fair Tree already recomputes every pending job's priority on every tick, so a switch adds one more pass of work the scheduler does each minute anyway.

**Recommendation: don't charge the switch pass.** A charge would be four orders of magnitude below the simulated time the virtual clock already charges. It would also be well inside the cost model's own error, since halving or doubling every cost moved the probe day's mean wait by about 3% (`results/vclock/summary.md`). It would need a new config key, which is a SPEC change. And the real cost is reported anyway: per switch as `swap_us` (§8 level 1), and per tick in the cost log (§8 level 3), which is where P5 measures it. What a switch does to the schedule comes from the reordering itself, and that is in the results with or without a charge. If the user wants a charge anyway, the measured line is about 5 µs + 0.4 µs per pending job, as a `SwapCost`/`SwapJobCost` pair in `sim.conf`. Nothing is charged now, and SPEC is unchanged.

## Finding: in the simulator the Age factor is always 0 and usage never decays

Found while placing the controller tick. Evidence: `decay_probe.txt` (stock `priority/multifactor`, contended trace, `DebugFlags=Priority`). The hand-over note is in the session notes folder (`a2-age-factor-frozen.md`).

- **Cause.** With `SLURM_SIMULATOR`, `_decay_thread()` sets its static `start_time = time(NULL)` only on its first call. The real-Slurm path refreshes it after each sleep, but the simulator path returns before reaching that line, and then calls the function again for every `PriorityCalcPeriod` event. So `start_time` stays at the run's first second. The bug is in stock multifactor and the fork alike.
- **Usage never decays.** `run_delta = start_time - g_last_ran` is 0 from the second call on, so the decay of past usage never runs, and `PriorityDecayHalfLife` has no effect. The probe logs no `Decay factor over` line at all. Running jobs get 0 s of usage per tick, and a job's whole run is added only when it ends.
- **The Age term is always 0.** Fair Tree is Slurm 23.11's default, and no `PriorityFlags` are set, so `fair_tree_decay(job_list, start_time)` runs every tick and does recompute every pending job's priority, but with the frozen `start_time`. Age is `start_time - accrue_time`, clamped at 0, so the Age term is 0 for every job. The probe shows job 1010 recomputed every minute for 29 minutes of waiting with an age term of 0.
- **What this means for the project.** Every Age weight is inert, and order comes from fair-share × `Fairshare` (plus JobSize in BASE), with ties going FIFO by job id. So the ten trigger policies collapse to two orderings:
  - `B` and `U` (`Fairshare` 0) are FIFO.
  - The eight with `Fairshare` > 0 all rank jobs by the same fair-share factor, only scaled.
  - Only `BASE` differs.

  P4's G5 check, and the churn and disruption numbers of every ordering switch (P5), would measure this artefact rather than SPEC §6's mapping.
- **A2 itself is unaffected.** A switch recomputes priorities with the new weights, and `fair_tree_decay` recomputes them again in the same tick with the same weights. So what A2 shows (switch times, recomputes, starts matching static U) holds either way. G1 holds because stock has the same code.
- **Proposed fix (not done; needs the user's decision).** In the simulator path of `_decay_thread()`, refresh `start_time = time(NULL)` on every call. Do it in stock multifactor (through `docker/patches/`) and in the fork, so G1 still compares like with like. Every virtual-clock baseline would change, so the vclock, A1 and A2 gates would be rerun and `DecayCost` refitted.

## Decisions taken in A2

Recorded in PROGRESS.md's decisions log.
- **Shared state:** a module compiled into slurmctld (above). The alternatives were a shared library both plugins link, which needs a build order across plugin folders, and finding the priority plugin's symbols with `dlopen(RTLD_NOLOAD)`, which depends on load order.
- **Windows win in every mode**, and afterwards the run goes back to its own policy. So SPEC §10's "Override only" run is `FixedPolicy=BASE` plus a schedule.
- **Cooldown:** counted in whole ticks; a window's end always starts a fresh one; a switch in AUTO needs a sample. `scripts/validate.py` now applies the same rules. It used to restart the cooldown only at switches and compare seconds exactly, so it would have rejected a correct log with a window longer than `TMin`.
- **Default schedule:** `config/adaptive.conf` now points at `config/schedule_none.conf`. Before, every run, the G1 checks and static runs included, would have had `config/schedule.conf`'s 12:00–17:00 → U window, and the probe day would have failed G1.
- **`swap_us`** covers the work under the job write lock, not the wait for it. **`pending_jobs`** is counted every tick, as logging; that count isn't a state sample.
- **Reconfigure** keeps the controller's state and the policy in force, if the new `policies.conf` still has it. Experiments don't use it; it matters for the stretch goal.
- **The swap's recompute uses the real simulated now**, not the decay loop's frozen `start_time`. In the simulator, `fair_tree_decay` recomputes right afterwards anyway, so the choice doesn't change any result.

## SPEC gaps (proposals only)

Proposals only; nothing in SPEC.md changed.
1. **§11, the simulator finding above:** decide whether to patch the frozen `start_time` in both priority plugins before P4. This is the one that matters most.
2. **§8, `sim_charge()` for the switch pass:** record the decision. The recommendation is not to charge.
3. **§13.1, windows in `FIXED` and `FORCED` mode:** say that a window wins in every mode, and what the run returns to afterwards (`FixedPolicy`; the first `ForceAlternate` policy, with the alternation restarting). §10's "Override only" row could then name its configuration: `FixedPolicy=BASE` plus `schedule.conf`.
4. **§13.1 and §13.3, the cooldown:** say that `TMin` and `ForcePeriod` are counted in whole ticks, that a window's end starts a fresh cooldown even when nothing switches, and that the window-end tick has `eligible` = 0.
5. **§13.3, `pending_jobs`:** say it is counted on every tick as logging, so it isn't a state evaluation under G4.
6. **§4 and §11, sharing the policy:** settle the A2 half. The policy state is a module compiled into slurmctld (`plugin/adaptive_policy/`), the controller publishes to it, and with nothing published the scorer uses stock placement. The A4 half is still open.
7. **§4.2 and §8, the queue-order dump for churn:** there is no format for it in §13, and P5 needs one. A2 doesn't write it.
8. **§13.1, `FORCED` phase:** say the first policy holds from the first tick for one `ForcePeriod`.
