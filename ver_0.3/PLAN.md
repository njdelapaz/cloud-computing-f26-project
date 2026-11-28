# PLAN — Development plan (ver_0.3)

This is the order of work for the `Project` brief. The phases match the brief: Phase 1 covers
Steps 1–3, Phase 2 covers Steps 4–5, and Phase 3 covers Step 6. Phase 4 revises the strategy
after Phase 3's answer (SPEC §9).

[SPEC.md](SPEC.md) specifies Steps 1–6. Each phase after the first started by writing its
part of the spec: Steps 4–5 in task 2.1, Step 6 in task 3.1.

Each task names what it produces and when it counts as done. The plan intentionally leaves out
durations and target dates. Status is tracked in `PROGRESS.md` (created in task 1.1). All work
stays inside `ver_0.3/`.

---

## Overview

| Phase | Steps | Outcome | Exit gate |
|---|---|---|---|
| 1 | 1–3: workloads, classes, weight configurations | Scenarios `K`, `N` and `X` built; 9 classes frozen; B0 and C0–C3 verified in the simulator | All Phase 1 "done when" items met |
| 2 | 4–5: trigger states and change mechanism | Triggers calibrated on K; policy module; simulator hook; apply test; adaptive week on K | 2.2–2.6 done; replay reproduces online decisions |
| 3 | 6: simulation of the full setup | Static and adaptive policies run on `N`, `X` and `Y`; answer to SPEC §1 by the SPEC §8.6 rules; scale-up on `M`; `REPORT.md` | 3.2–3.8 done (met 2026-11-27 with 3.7 skipped on request) |
| 4 | revised strategy (after the Phase 3 answer) | B1 baseline with favored medium and large spells, triggered by wait targets, each followed by a stabilization period; one-day setup workloads `D`; target level and stabilization period chosen; targeted-job waits while active | 4.2–4.7 done; S-A to S-G answered |

---

## Phase 1 — Workloads, classes, weight configurations (Steps 1–3)

### 1.1 Folder setup and tooling
- Create the layout under [Working conventions](#working-conventions).
- Extend `ver_0.3/.gitignore` so it also covers the Parquet cache in `workloads/cache/`.
- Create `.venv` with a pinned `requirements.txt`.
- Create `PROGRESS.md` with a task tracker, decisions log and change log.

**Done when:** `pytest` runs (an empty suite passes); `git status` shows no generated files;
`PROGRESS.md` lists every Phase 1 task.

### 1.2 Simulator bring-up
- Fetch the UB Slurm Simulator source into `third_party/` at a pinned commit, and build the
  container image from `sim/`.
- Write `sim/slurm.conf` for the SPEC §2 cluster (16 × 32 cores, 128,000 MB, `normal`
  partition and QOS, `cons_tres` + `CR_Core_Memory`, `priority/multifactor`,
  `PriorityCalcPeriod=1`, `Include priority_weights.conf`) and the `sacctmgr` setup script
  (SPEC §2.3).
- Smoke run: a few dozen hand-written jobs. Confirm the `events.txt` line format (SPEC §3.6),
  `--mem`, `-t` in minutes, per-job `--uid`/`-A`, and that `sacct` returns submit, start, end,
  node and allocation for every job.
- **Mid-run probe**, recorded as input for the Step 5 spec. On a run with a standing queue:
  1. Can `squeue`, `sprio`, `sshare` and `scontrol show node` be queried mid-run?
  2. After rewriting `priority_weights.conf` and running `scontrol reconfigure`, does
     `scontrol show config` show the new weights, and do `sprio` values change within one calc
     period?
  3. Do the queue, running jobs, accounting and the simulated clock survive the reconfigure?
  4. How can an outside process read simulated time?
- Write `sim/README.md` with the build and run commands, the pinned commit, Slurm version and
  image digest, and the probe results.

**Done when:** the smoke run completes and its `sacct` output matches the input jobs; the probe
answers are recorded in `sim/README.md` and `PROGRESS.md`.

### 1.3 Trace profiling
- Convert `dataset/*.tsv` once to a Parquet cache in `workloads/cache/`. `dataset/` is never
  modified.
- Check the SPEC §3.1 assumptions: memory units, UTC timestamps, `hostnames` completeness, and
  `elapsed`/`timelimit` consistency.
- Build a weekly profile across all six months: filtered job count, `ρ_cpu` and `ρ_mem` before
  thinning, class mix, active accounts and users. Save it as a table and plots.
- Pick the calibration week `K` and evaluation week `N`, and confirm or adjust the class
  thresholds.

**Done when:** the profile table is committed under `results/`; the weeks are recorded in
SPEC §3.5 and the thresholds in SPEC §4; every assumption is marked confirmed or corrected.

### 1.4 Job classifier
- Put the thresholds in `config/classes.yaml` and implement the classification function
  (SPEC §4).
- Write tests at every boundary (exactly 2 cores, 8,000 MB, 4 h, 24 h, and so on), including
  jobs that are memory-bound and CPU-bound.
- Produce a per-class summary table (jobs, core-hours, memory-hours) for any job table.

**Done when:** the tests pass, and the January summary reproduces the SPEC §4.3 shares (or the
spec is updated with justification).

### 1.5 Workload builder
- Implement the filters, field mapping, thinning solver for `ρ*` and optional class quotas
  (SPEC §3.2–3.4).
- Implement scenario `X`: segment selection from trace jobs, re-timing, and per-segment load
  (SPEC §3.5).
- Write the outputs: `jobs.parquet`, `events.txt`, `accounts.txt`, `manifest.json`
  (SPEC §3.6).
- Build `K`, `N`, `X` and `Y` for 3 seeds each. `M` waits until one-week runs work.
- Tests cover:
  - determinism: same seed, same bytes;
  - achieved `ρ` within ±2% of `ρ*`;
  - every job fits one node;
  - every limit ≤ 7 days and every `runtime_s ≤` limit;
  - every user/account pair appears in `accounts.txt`;
  - each `X` segment's dominant class or account matches its intent.
  - each `Y` burst sits in its window with the jobs its pattern calls for.

**Done when:** all scenario directories exist with manifests, and the tests pass.

### 1.6 Weight configurations
- Write `config/weights.yaml` with the tiers, goal-to-factor map, TRES split and the
  size-QOS configurations C0–C3 (favor none / large / small / medium, then shortest time limit, then
  fairness; since 2026-11-26) (SPEC §5.1–5.3).
- Write the renderer that produces `config/priority/{B0,C0..C3}.{conf,qos}`.
- Compute the TRES and job-size factor spreads (`s_cpu`, `s_mem`, `s_jobsize`) from scenario
  `K` (SPEC §5.4). Leave the fair-share spread provisional until task 1.7.

**Done when:** the renderer output is checked by tests (only `PriorityWeight*` keys; integer
weights; tier order holds for each configuration).

### 1.7 Configuration check in the simulator
The simulator answers no CLI calls while it runs (task 1.2 probe), so priorities are read from
the `DebugFlags=Priority` lines in `slurmctld.log` instead of `sprio`. Criteria for the current
size-QOS configurations are in SPEC §5.5: favored class first in every cross-class pair; time
limit ahead of fairness within a class; cluster-state numbers reported.
- Run a slice of `K` with a standing queue under each rendered file, with
  `DebugFlags=Priority`. Confirm the logged weights match the file.
- From the logged factors, measure the fair-share spread, re-render the weights, and confirm
  the primary goal's factor makes the largest spread of contributions in each configuration
  (SPEC §5.5).
- Record the U–L contribution correlation (SPEC §5.5).

**Done when:** the per-configuration priority summaries are committed; final weights are
rendered; the correlation is recorded, with a note if `|r| > 0.8`.

**Phase 1 exit:** tasks 1.1–1.7 are done; class thresholds, weeks and weights are frozen in
SPEC.md; `PROGRESS.md` is updated.

---

## Phase 2 — Trigger states and change mechanism (Steps 4–5)

### 2.1 Specify Steps 4–5
- Add SPEC sections for:
  - the cluster states that trigger a configuration change;
  - the change mechanism.
- Build on the Phase 1 results: the probe answers from 1.2, the verified configurations from
  1.7, and the scenarios from 1.5.
- Use these queue scenarios (set 2026-11-26) as the states that select a configuration:
  many large jobs waiting → C1 (favor large); many small → C2 (favor small); many medium →
  C3 (favor medium); a mix → C0 (neutral). Step 4 defines how "many" is measured, for
  example each size class's share of the waiting demand.
- Measure load balance and utilization on cluster state (per-node utilization), not on the
  queue.
- Then expand the rest of Phase 2 in this plan into tasks with "done when" items.

**Done when:** the new SPEC sections and the expanded Phase 2 tasks are agreed and committed.
(Drafted 2026-11-26: SPEC §6–7 and tasks 2.2–2.6 below.)

### 2.2 Trigger calibration (Step 4)
- Run the full `K-s1` week under C0 (neutral). That is one week-long simulation, about 2 h.
- From it, record the waiting-queue share of each size class every 10 simulated minutes.
  Write the percentile tables and the SPEC §6.6 parameters to `config/triggers.yaml`.
- Report how often each state would be active in that week, and how often the state would
  change, under the default thresholds.

**Done when:** `config/triggers.yaml` and the state-frequency summary are committed.

### 2.3 Policy module (Step 4)
- Write `controller/policy.py`, the pure decision function of SPEC §6.4, and
  `controller/tick.py`, the command the simulator runs (SPEC §7.2).
- Write unit tests on synthetic snapshots:
  - each state is selected;
  - the gate and quiet `mix` hold;
  - the persistence and dwell rules block early switches;
  - a state is left when its percentile drops below the exit level;
  - the log record matches SPEC §7.5.
- Write a replay tool that runs the policy over a run's logged snapshots.

**Done when:** the tests pass. This can run in parallel with 2.2 and 2.4.

### 2.4 Simulator hook (Step 5)
- Write `sim/patches/policy_hook.patch` against the pinned `slurm_simulator`, applied in
  `sim/Dockerfile`. It adds:
  - the `SIM_POLICY_TICK` event;
  - the snapshot writer;
  - the blocking `PolicyCommand` call;
  - the apply step (weights plus priority-plugin reconfigure, QOS priorities through the
    accounting manager, immediate priority recalculation);
  - the `set_priority_config` event in the events-file parser.
- Add the `sim.conf` settings and the `sim/prepare_run.py` options to use them.
- Rebuild the image. The smoke and probe runs must still pass with the hook off.

**Done when:** the image builds, and the smoke run and a static check run give the same
results as before.

### 2.5 Apply test (Step 5)
- Run scheduled switches C0 → C1 → C2 → C3 → C0 on a `K` slice with `DebugFlags=Priority`.
- Within one simulated minute of each switch, the logged weights and QOS factors must equal
  the new configuration's (SPEC §7.6 item 2).
- An injected failure (a missing configuration file, or invalid command output) must leave
  the configuration unchanged and be logged.

**Done when:** all switches are confirmed in the log and the failure cases behave.

### 2.6 Adaptive week (Steps 4–5 together)
- Run the full `K-s1` week with the policy active.
- Check that:
  - every tick is logged;
  - each switch shows up in the priority debug log;
  - replay reproduces every online decision;
  - the run check passes.
- Report the switches per day, time in each state, and reversals.

**Done when:** all checks pass and the summary is committed.

**Phase 2 exit:** 2.2–2.6 done; open questions Q-A to Q-C (SPEC §6.6) answered or kept at
their defaults; `PROGRESS.md` updated. (Met 2026-11-26; the trigger feedback finding in SPEC
§6.5 is an input to 3.1.)

---

## Phase 3 — Simulation of the full setup (Step 6)

(Drafted and approved 2026-11-26: SPEC §8 and tasks 3.2–3.8 below, with the default
answers to SPEC §8.9.)

### 3.1 Specify Step 6
- Add the SPEC section for simulation and evaluation: what is compared, which metrics, how
  many runs, and what counts as "effective".
- Include the trigger variants worth comparing against the Phase 2 trigger: the arrivals
  signal (SPEC §6.6, Q-A) and longer minimum stays for small and medium (SPEC §6.5).
- Then expand the rest of Phase 3 in this plan.

**Done when:** the new SPEC section and the expanded Phase 3 tasks are agreed and committed.

### 3.2 Evaluation tooling
- Add B1 (SPEC §8.2):
  - `sim/weights.py` renders `config/priority/B1.conf` (its weights and static keys) and
    `B1.qos` (all QOS priorities 0) alongside B0, so a re-render keeps them;
  - `sim/prepare_run.py` writes B1's static keys in place of the template's lines.
- Give `sim/prepare_run.py` a triggers file per adaptive variant: `config/triggers.yaml`
  (A1), `config/triggers_A2.yaml` (per-state minimum stays) and `config/triggers_A3.yaml`
  (arrivals, written in 3.3).
- Write `sim/batch.py`, which runs a list of (workload, policy) runs:
  - one batch per workload, with a limit on how many run at once;
  - resumable: it skips runs that already finished and passed their checks;
  - it runs each run's checks (SPEC §8.4) and records the outcome.
- Write `evaluation/metrics.py`, every SPEC §8.5 metric for one run, from its sacct output,
  the workload's `jobs.parquet` and, for adaptive runs, the controller log.
- Write `evaluation/compare.py`: noise bands, `S*`, paired differences and the SPEC §8.6 pass
  rules.
- Write tests on synthetic tables: wait and bounded slowdown, Jain's index, per-day and
  per-burst splits, the noise band and each pass rule.

**Done when:**
- the tests pass;
- the metrics of the Phase 2 `K` runs match the numbers already reported (mean wait 120 min
  under C0, 84 min adaptive);
- a short B1 run on a `K` slice with `DebugFlags=Priority` logs B1's weights, with job-size
  factors that grow with the request, not with a shorter time limit.

### 3.3 Arrivals variant (A3)
- Extend the hook (SPEC §8.3): count each submitted job by its size QOS, and add the running
  totals (`submitted`) to `state.json`. Rebuild the image.
- Give the policy a `signal` parameter (`waiting` or `arrivals`) and the arrivals gate.
- Calibrate the arrival-share percentiles from the `K-s1` workload. Write
  `config/triggers_A3.yaml`.
- With the rebuilt image, rerun:
  - the hook-off smoke check;
  - the apply test;
  - a short A3 run on a `K` slice, where arrival counts must match sacct at every tick and
    replay must be exact.

**Done when:** these checks pass. The rebuilt image is used for every run from 3.4 on.

### 3.4 Pilot
- Run C0 and A1 alone on seed 1 of `N`, `X` and `Y` (6 runs).
- Then run the `N-s1` batch of nine policies together (it is also that workload's matrix
  batch in 3.5).
- Record each run's real time, peak memory and CPU, and disk use.

**Done when:** the batch size is set (nine at once, or fewer if the host cannot carry nine)
and the run costs are in `PROGRESS.md`.

### 3.5 One-week evaluation runs
- Run the matrix: 5 policies (B1, C0, A1–A3; SPEC §8.2) × 9 workloads, one batch per workload.
- Run the 6 extra repeat runs: C0 and A1 on seed 1 of each scenario.
- Every run passes its checks, or is rerun once, or is excluded and listed (SPEC §8.4).
- Write `results/phase3/metrics.csv`, one row per run.

**Done when:** all 57 runs are accounted for and `metrics.csv` is committed.

### 3.6 Analysis of the one-week results
- Compute the noise bands, `S*`, the paired differences and the pass rules. Write
  `results/phase3/comparison.json`.
- Figures in `results/phase3/`:
  - mean wait by policy and scenario;
  - per-class waits;
  - the adaptive state over the `X` week against its day patterns;
  - burst and background waits in `Y`.

**Done when:** `comparison.json` and the figures are committed, and the SPEC §8.6 answer is
recorded in `PROGRESS.md`.

### 3.7 Scale up (`M`)
- Add `M` to `config/scenarios.yaml` (weeks 11–14, SPEC §8.7) and build seeds 1–3.
- Run `S*`, C0 and the best adaptive variant (and A1 if it is not the best): 9–12 runs.
- Report each policy's metrics and, for each week of `M`, how well `K`'s percentile tables
  fit.

**Done when:** the runs pass their checks and their results are committed.

### 3.8 Report
- Write `REPORT.md`:
  - the answer to SPEC §1 by the SPEC §8.6 rules;
  - the one-week and `M` results;
  - the limitations listed in SPEC §8.8.

**Done when:** `REPORT.md` is committed.

**Phase 3 exit:** 3.2–3.8 done; open questions E-A to E-E (SPEC §8.9) answered or kept at
their defaults; `PROGRESS.md` updated. (Met 2026-11-27: 3.7 skipped on request after the
one-week answer was no; `REPORT.md` written.)

---

## Phase 4 — Revised strategy: B1 baseline with a stabilization period (one-day setup)

(Drafted and approved 2026-11-27, requested after the Phase 3 answer was no: SPEC §9 and
tasks 4.1–4.7 below. Open questions in SPEC §9.9: S-B, S-C and S-G answered; defaults kept for
S-A, S-D, S-E and S-F.)

### 4.1 Specify the revised strategy
- Add SPEC §9:
  - the B1-based states (no small state);
  - the trigger: a class's waiting jobs have waited past a target (S-B, answered), then a
    fixed favored spell, a return to B1 and a stabilization period;
  - the one-day setup workloads;
  - what is compared, the metrics (including targeted jobs while their state is active),
    and how the target level and the stabilization period are chosen.
- Expand Phase 4 in this plan.

**Done when:** SPEC §9 and tasks 4.2–4.7 are agreed and committed.

### 4.2 One-day setup workloads (`D`)
- Add a `day` section to `config/scenarios.yaml` (SPEC §9.4): the seven patterns, the
  12-hour natural lead-in, and the source weeks 1–12.
- Extend `workloads/build.py` to build `D-<pattern>-s<seed>` with the same outputs as
  SPEC §3.6. Every job carries its `segment` (`leadin` or the pattern).
- Write tests on a small pool:
  - lead-in and day windows;
  - each pattern's class filter;
  - source weeks only from 1–12;
  - outputs byte-identical for the same seed.

**Done when:** the 21 workloads are built and deterministic, each day's load is near `ρ*`
in its saturated resource, and the manifests are in `results/workloads/`.

### 4.3 B1-based favored states
- Add the `favored_on` block for B1 to `config/weights.yaml`. `sim/weights.py` renders
  `config/priority/B1M.*` and `B1L.*` with B1's weights plus `PriorityWeightQOS = 5000`,
  and only the favored class's QOS raised (SPEC §9.2).
- Let `sim/prepare_run.py` give an adaptive run a static base (`static: B1`), so B1's
  static keys hold for the whole run.
- Check on a `D-nat` slice (run with `DebugFlags=Priority`):
  - a scheduled switch B1 → B1L → B1 → B1M is applied each time, with the logged weights
    and QOS factors;
  - while B1M or B1L is active, favored jobs carry a QOS component of 5,000 and the others
    0;
  - a favored job comes first in every cross-class pair whose B1 priority gap is under
    5,000 (the share of cross-class pairs where it comes first is reported);
  - within a class, B1's order holds.

**Done when:** the check passes and its summary is in `results/phase4/states_check.json`.

### 4.4 Return-to-baseline policy (A4)
- Extend `controller/policy.py` (SPEC §9.3):
  - a `wait_target` trigger: per candidate class (medium, large), the median `waited_s` of
    its waiting jobs against `τ_c`, with the class gate `min_share` (the class's share of
    the waiting jobs), the whole-queue gate `n_min`, and the `w_c / τ_c` tie-break;
  - a fixed `active_s` (one value, or one per state);
  - `stable_s` and the phases `armed`, `active` and `stabilizing`;
  - the streak reset when the stabilization period ends.
- Update `controller/tick.py`, `replay.py` and `summarize.py` for the per-class waits, the
  phases and the new log fields. The A1–A3 behavior stays unchanged.
- Write `config/triggers_A4.yaml` with the three target levels (SPEC §9.3).
- Write unit tests:
  - the median wait, the class-share gate and the whole-queue gate, on synthetic snapshots;
  - the tie-break when both classes are over target;
  - entry only from `armed`;
  - a fixed spell, with no early exit and no switch to the other class;
  - the return to B1, and no trigger during stabilization;
  - `T_stable = 0`;
  - per-state `active_s`.

**Done when:** the tests pass, the Phase 2–3 policy tests still pass, and replay of an A4
slice run on `D-alt` is exact.

### 4.5 Stage 1: target level
- Write `config/evaluation_setup.yaml` (SPEC §9.5, §9.7). Point `sim/batch.py` and
  `evaluation/metrics.py` at it, with metrics on the measured day's jobs only and cluster
  state over the measured 24 h.
- Run B1, C0, A4-tight-60, A4-middle-60 and A4-loose-60 on the 21 workloads (105 runs), one
  batch per workload.
- Run the 28 repeat runs (B1 and A4-middle-60 on seed 1 of each pattern).
- Every run passes its checks, is rerun once, or is excluded and listed (SPEC §8.4).
- Compute the noise bands, the paired ratios against B1, the guards, and each level's score.
  Choose `L*` by the SPEC §9.6 rule.
- Report each level's spells per day and time active by pattern. A level that never fires,
  or is active most of the day, is flagged.

**Done when:** all 133 runs are accounted for, and `metrics.csv` and the stage 1 part of
`results/phase4/selection.json` are committed with `L*` recorded in `PROGRESS.md`.

### 4.6 Stage 2: stabilization period
- Run A4-`L*`-`T` for `T` ∈ {0, 30, 120, 240, 480} on the 21 workloads (105 runs).
- Same checks as 4.5.
- Choose `T_stable` by the SPEC §9.6 rule, with the A4-`L*`-60 runs from stage 1 included.

**Done when:** all 105 runs are accounted for, and `metrics.csv` and `selection.json` are
updated and committed with `T_stable` recorded in `PROGRESS.md`.

### 4.7 Analysis of the setup
- Extend `evaluation/favored.py` to the phases. For every A4 setting, compare these groups
  with B1, and write `results/phase4/favored.json`:
  - targeted jobs while their state is active;
  - other jobs while it is active;
  - jobs during the stabilization period.
- Report `w_c` at each trigger and at the end of its spell.
- Figures:
  - score against target level and against `T_stable`;
  - mean wait by policy and pattern;
  - targeted-job waits while active;
  - the phase timeline on `D-alt` and `D-over`.
- Write the setup result into SPEC §9.6.

**Done when:** `favored.json` and the figures are committed, and the setup result is in
SPEC §9.6 and `PROGRESS.md`.

**Phase 4 exit:** 4.2–4.7 done; S-A to S-G answered or kept at their defaults;
`PROGRESS.md` updated. The five-day tests (SPEC §9.8) are specified next, as Phase 5.

---

## Dependencies

| Task | Needs |
|---|---|
| 1.2, 1.3 | 1.1 |
| 1.4 | 1.3 |
| 1.5 | 1.4 |
| 1.6 | 1.5 |
| 1.7 | 1.2, 1.6 |
| 2.1 | Phase 1 complete |
| 2.2, 2.3, 2.4 | 2.1 (can run in parallel) |
| 2.5 | 2.4 |
| 2.6 | 2.2, 2.3, 2.5 |
| 3.1 | Phase 2 complete |
| 3.2, 3.3 | 3.1 (can run in parallel) |
| 3.4 | 3.2, 3.3 |
| 3.5 → 3.6 → 3.7 → 3.8 | 3.4 |

Task 1.2 can run in parallel with 1.3–1.6.

---

## Decisions for Phase 1

| Decision | Decided in | Proposed default |
|---|---|---|
| Class thresholds | 1.3 | Small `f ≤ 1/16`, medium `≤ 1/4`; short ≤ 4 h, medium ≤ 24 h (SPEC §4) |
| Calibration and evaluation weeks | 1.3 | Two different typical weeks, not the Jan 1 week (SPEC §3.5) |
| Target load | 1.5 | `ρ* = 0.95` on the bottleneck (memory) dimension (SPEC §3.4) |
| Fairness entity | fixed | Account first, user second (SPEC §2.3) |
| Placement | fixed | Priority weights only; placement untouched (SPEC §1) |
| Weight tiers and TRES split | 1.6–1.7 | 4000 / 2000 / 1000, Age 500; CPU/Mem 50/50 (SPEC §5.2–5.4) |

Decisions for Steps 4–6 are made when their spec is written (2.1, 3.1).

---

## Risks (Phase 1)

| # | Risk | Mitigation |
|---|---|---|
| R1 | The simulator cannot answer CLI calls or apply weight changes mid-run. | Probe in 1.2, so the Step 5 spec is written knowing the answer. |
| R2 | Trace fields mean something other than assumed (units, multi-node). | Checks in 1.3, before any workload is built. |
| R3 | One factor swamps the others because of normalization (TRES spans only 0–1/16). | Spread-based weight calibration (SPEC §5.4), verified from the priority debug log in 1.7. |
| R4 | Utilization and load-balance factors are nearly opposite, so configurations collapse. | `SMALL_RELATIVE_TO_TIME` (SPEC §5.1); correlation check in 1.7; recorded as a finding if it happens. |
| R5 | Too little contention in the built workloads, so priority order barely matters. | `ρ* = 0.95`; scenario `X` has built-in peaks; the 1.7 priority check needs a standing queue. |
| R6 | Thresholds or weights get tuned on the weeks used for judging. | All calibration on `K` only (SPEC §3.5). |

## Risks (Phase 3)

| # | Risk | Mitigation |
|---|---|---|
| R7 | Run-to-run variation hides real differences (two FIFO runs of one slice: 88 vs 51 min mean wait). | Paired comparisons on the same workload, three seeds, a measured noise band (SPEC §8.6). "No difference beyond noise" is a valid answer and is reported as such. |
| R8 | Host load changes how the simulator behaves. | All policies of a workload run in one batch, so they share conditions. The noise band includes a run made alone (SPEC §8.4). |
| R9 | `X` and `Y` runs are slow or heavy (up to 3.4 times `K`'s jobs). | The pilot (3.4) measures them before the matrix and sets the batch size. |
| R10 | The arrivals signal is noisy for large jobs, a few an hour. | 60-minute window and an arrivals gate (SPEC §8.3). Reported as is; no tuning on evaluation weeks. |

## Risks (Phase 4)

| # | Risk | Mitigation |
|---|---|---|
| R11 | One-day runs are noisier than weeks, and B1 swung widely by seed on `X` (54 vs 900 min). | Noise band per pattern from three runs of B1 and A4-middle-60 (SPEC §9.6). The target level and `T_stable` are each chosen on all 21 workloads together. |
| R12 | The target level and `T_stable` are tuned on the setup workloads. | Setup and tests draw from different months (weeks 1–12 against 13–23, SPEC §9.4); the five-day tests (Phase 5) judge the choice. |
| R13 | Fair share under B1 depends on usage history, which a one-day run barely has. | The 12-hour lead-in (S-D); reported as a limitation; the five-day tests carry more history. |
| R14 | A target level never fires on some patterns, or keeps a class favored most of the day. | Three levels a factor of two apart (SPEC §9.3); stage 1 reports spells and time active per pattern and flags both cases (task 4.5). |

---

## Working conventions

### Layout (all inside `ver_0.3/`)

```
ver_0.3/
├── Project              brief (input; unchanged)
├── SPEC.md  PLAN.md     spec and development plan
├── PROGRESS.md          status tracker (created in 1.1)
├── .gitignore           dataset/, .venv/, third_party/, workloads/traces/, workloads/cache/, raw run output
├── requirements.txt     pinned Python dependencies
├── dataset/             raw trace (read-only input; gitignored)
├── third_party/         pinned simulator checkouts (gitignored)
├── sim/                 container build, slurm.conf, sacctmgr setup, run scripts, policy hook patch, README
├── controller/          queue-state policy, tick command, replay, trigger calibration (Phase 2)
├── evaluation/          run metrics and comparisons (Phase 3)
├── workloads/           profiler, builder, classifier; cache/ (Parquet), traces/ (scenarios)
├── config/              classes.yaml, weights.yaml, priority/*.conf, triggers*.yaml
├── results/             summaries (committed); results/<group>/<run>/ folders and logs (gitignored)
└── tests/               unit and integration tests
```

`controller/` was added in Phase 2 and `evaluation/` is added in Phase 3.

### Practices
- Python 3.12 in `ver_0.3/.venv`, with pinned `requirements.txt` (pandas, pyarrow, numpy,
  pyyaml, matplotlib, pytest).
- All randomness is seeded, and every generated artifact has a manifest.
- Thresholds, tiers and weights live in `config/*.yaml`, never in code.
- Generated data stays out of git. Manifests and summaries are committed so every result can be
  traced to a commit, seed and image.
- One commit per finished task on the `ver_0.3` branch, with `PROGRESS.md` updated in the same commit (status,
  decisions, results; no durations).
- Nothing outside `ver_0.3/` is read, written or referenced.
