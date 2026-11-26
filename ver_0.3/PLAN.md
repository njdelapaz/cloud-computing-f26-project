# PLAN — Development plan (ver_0.3)

This is the order of work for the `Project` brief. The phases match the brief: Phase 1 covers
Steps 1–3, Phase 2 covers Steps 4–5, and Phase 3 covers Step 6.

[SPEC.md](SPEC.md) currently specifies Steps 1–3 only. Steps 4–6 are specified after Phase 1
is complete, so Phases 2 and 3 are outlined here and each starts by writing its part of the
spec.

Each task names what it produces and when it counts as done. The plan intentionally leaves out
durations and target dates. Status is tracked in `PROGRESS.md` (created in task 1.1). All work
stays inside `ver_0.3/`.

---

## Overview

| Phase | Steps | Outcome | Exit gate |
|---|---|---|---|
| 1 | 1–3: workloads, classes, weight configurations | Scenarios `K`, `N` and `X` built; 9 classes frozen; B0 and C0–C3 verified in the simulator | All Phase 1 "done when" items met |
| 2 | 4–5: trigger states and change mechanism | Spec for Steps 4–5 written, then built and checked in the simulator | Defined when the Step 4–5 spec is written |
| 3 | 6: simulation of the full setup | Spec for Step 6 written, then the runs, analysis and report | Defined when the Step 6 spec is written |

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

### 2.2 Determine trigger states (Step 4)
Build and check the trigger states as specified in 2.1.

### 2.3 Build the change mechanism (Step 5)
Build the mechanism as specified in 2.1, then check that it changes weights correctly inside
the simulator.

**Phase 2 exit:** set by 2.1.

---

## Phase 3 — Simulation of the full setup (Step 6)

### 3.1 Specify Step 6
- Add the SPEC section for simulation and evaluation: what is compared, which metrics, how
  many runs, and what counts as "effective".
- Then expand the rest of Phase 3 in this plan.

**Done when:** the new SPEC section and the expanded Phase 3 tasks are agreed and committed.

### 3.2 Run the simulations
Run the one-week workloads with the full setup, as specified in 3.1.

### 3.3 Scale up
Build scenario `M` (SPEC §3.5) and repeat on the longer horizon, as the brief says: one week
first, scale later.

### 3.4 Analyze and report
Analyze the results and write `REPORT.md`, which answers the research question in SPEC §1.

**Phase 3 exit:** set by 3.1.

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
| 2.2, 2.3 | 2.1 |
| 3.1 | Phase 2 complete |
| 3.2 → 3.3 → 3.4 | 3.1 |

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
├── sim/                 container build, slurm.conf, sacctmgr setup, run scripts, README
├── workloads/           profiler, builder, classifier; cache/ (Parquet), traces/ (scenarios)
├── config/              classes.yaml, weights.yaml, priority/*.conf
├── results/             summaries (committed); results/<group>/<run>/ folders and logs (gitignored)
└── tests/               unit and integration tests
```

Folders for Steps 4–6 are added when their spec is written.

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
