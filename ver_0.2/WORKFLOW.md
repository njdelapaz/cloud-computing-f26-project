# Workflow

How to get from `SPEC.md` to results. Phases are **gated**: each ends with an artifact plus a command that reproduces it, and the next phase starts only once that gate passes.

## Principles
1. **Scope:** all work stays inside `ver_0.2/`.
2. **Spec first:** `SPEC.md` holds the what and why, and changes only by an explicit decision, which gets logged in `PROGRESS.md`.
3. **Freeze interfaces early:** config formats, trace format, decision-log schema and metric definitions are settled before the C and Python work splits into two tracks.
4. **Validate pieces before combining them:**
   - both plugins must match stock multifactor and cons_tres before they adapt
   - policies must do what their names say in static runs before adaptive runs
   - thresholds are calibrated on seeds kept separate from evaluation seeds
5. **Reproducible:** every experiment is one command from config to plots, and results are never edited by hand.
6. **Tracking:** `PROGRESS.md` records status, decisions, results and a change log, with no time estimates.

## Phase map
```
P0 Spec ─► P1 Simulator env ─► P2 Interfaces ─┬─► Track A: plugins (A1 ─► A2 ─► A3 ─► A4) ─┬─► P4 Static policies ─► P5 Switch overhead ─► P6 State calibration ─► P7 T_min ─► P8 Evaluation ─► P9 Report
                                              └─► Track B: tools   (B1 ─► B2 ─► B3)        ─┘
```
Tracks A and B touch disjoint folders (`plugin/` vs `workloads/`, `analysis/`, `experiments/`), so they can run in parallel.

## Phases and gates

| Phase | Work | Gate |
|---|---|---|
| **P0 Spec** | Answer SPEC §11 open questions; merge the draft spec into `SPEC.md`; create `PROGRESS.md` | No open question left that blocks P1–P2 |
| **P1 Simulator env** | Docker build of the newer UB simulator (Slurm 23.x) in `docker/`; record the exact Slurm release. Tiny cluster config and a hand-written ~10-job trace. Run the same trace 3× to check determinism. Find which clocks the simulator virtualizes, and confirm it tracks memory as a consumable resource (`CR_Core_Memory`) | One script runs the smoke trace. A hand edit to `PriorityWeight*` in `slurm.conf` changes start order exactly as predicted. No-op forks of both plugins build and run. Clock and memory-tracking findings recorded |
| **P2 Interfaces** | Example `policies.conf`, `schedule.conf`, `adaptive.conf`, neutral trace schema, decision-log and placement-log CSV schemas, metric definitions | Python validators accept every example file. Schemas are recorded in SPEC |
| **A1 Ordering plugin: static** | Fork `priority/multifactor` → `priority/adaptive` that loads one policy's ordering weights from `policies.conf` | With `BASE` loaded, matches stock multifactor (G1): **exactly** (start, end and node) on the smoke trace, the contended trace and the probe day, on the virtual clock |
| **A2 Switching** | Shared policy state in slurmctld, hot swap, schedule/override, cooldown, decision log. `decide()` as a pure function. Decide whether the switch pass charges a fixed simulated cost (SPEC §8) | Unit-test table passes, covering override boundaries, cooldown skip with no sampling, and override-vs-cooldown rule. A scripted-window trace switches at the expected sim times (G2) |
| **A3 States** | Snapshot, the four signals, hysteresis, severity ranking → policy | Unit tests on synthetic snapshots pick the expected policy for each case |
| **A4 Placement plugin** | Fork `select/cons_tres` → `select/cons_tres_adaptive`. `score()` as a pure function (SPEC §6.1), used for real starts and backfill tests, reading placement weights from the shared policy state. Optional placement log. Decide whether scoring charges a fixed simulated cost per node choice (SPEC §8) | With placement weights 0, matches stock cons_tres (G1): **exactly** (start, end and node) on the smoke trace, the contended trace and the probe day, on the virtual clock. `score()` unit tests pass, including the SPEC §6.1 example. In a scripted test, backfill's planned node and the real start's node agree |
| **B1 Workloads** | Trace filter, random-job sampler and simulator converter (SPEC §9.2). Episode finder for W1, W2, W4 and W6, with onset sidecars (§9.3) | Filtered-trace job and core-hour counts match SPEC §9.2. Each sample's offered load is within target. Episodes reviewed, with thresholds adjusted if they don't look like real floods or bursts, and the list recorded in PROGRESS |
| **B2 Metrics** | Metrics from completion logs, decision log and placement log | Metrics on a tiny hand-computed trace match the hand calculation |
| **B3 Runner** | Run-matrix config → simulator runs → `results/<run-id>/`. Resumable, records git state and seed | One command runs a 2×2 mini-matrix end to end |
| **P4 Static policies** | Tune the policy mapping (SPEC §6: Fairshare scale, Age floor, composite split, scorer terms), derive the 10 policies, run static × W0–W6 × calibration seeds | **G5:** each single-metric policy leads on its own metric. If not, retune and repeat. Final values go in the PROGRESS decisions log |
| **P5 Switch overhead** | Forced F↔U alternation at each period; swap/sample cost vs pending count; placement scoring cost vs stock selection; churn | Overhead plots plus a cost-of-switching curve. X and Y for the T_min rule chosen from this data and written into SPEC §8 |
| **P6 State calibration** | Set `Enter`/`Exit` from `BASE` signal distributions (calibration seeds). Find W3 and W5 episodes in the `BASE` runs | Detection table (workload × state fired × response latency): W1–W3 fire their intended state, W0 stays quiet (G4). W3 and W5 episodes listed, or recorded as not found |
| **P7 T_min** | Sweep T_min in auto mode, applying the rule fixed at the end of P5 | Chosen T_min, the plot, and the rule applied, all in PROGRESS (G3) |
| **P8 Evaluation** | Full matrix on **held-out** seeds (March–June windows) | One command regenerates tables and plots with CIs (G7) |
| **Stretch: reconfigure baseline** | Switch policies by `scontrol reconfigure` with a swapped config, measured the same way as P5 | Hot swap vs reconfigure overhead table |
| **P9 Report** | Slides and write-up built from `analysis/` outputs only | Every number in the report traces back to a results file |

## Working with Claude
- **One session per phase or track.** After P2, Track A and Track B can be separate sessions in parallel, because their folders don't overlap.
- **Session start:** read `SPEC.md`, `WORKFLOW.md` and `PROGRESS.md`, and nothing outside `ver_0.2/`.
- **Session end:** update `PROGRESS.md` (status, decisions, results) and commit on `dev`, one commit per gate.
- **Spec changes:** raised as a question, then recorded as a decision, then edited into `SPEC.md`. Never changed silently mid-task.
- **Gate checks** run as scripts (for example `scripts/check_<phase>.sh`), so anyone can re-run them.

## Risks to retire early
| Risk | Where it gets caught |
|---|---|
| The simulator's Slurm version has a different plugin API than current docs | P1: build trivial no-op forks of both plugins first |
| Forking `select/cons_tres` means changing large, intricate code | A4 changes only how a node is chosen for single-node jobs and leaves the rest of cons_tres untouched; the G1 identity check catches side effects |
| Backfill plans and real starts disagree on nodes | A4 uses one `score()` for both paths, with a scripted agreement test |
| Time virtualization makes `swap_us` and `score_us` meaningless | P1 clock check; fall back to offline microbenchmarks of the recompute pass and the scorer |
| Simulator runs aren't repeatable on realistic load (found in A1: identical runs of one day differ on most jobs) | Retired: the virtual clock and the stale-event fix make identical runs byte-identical (SPEC §11 #23, `scripts/check_vclock.sh`) |
| The virtual clock's fixed costs misstate scheduler time, or plugin overhead goes missing from simulated time | Costs are fitted from real-clock runs, and doubling or halving them moved mean wait about 3%. Refit with `scripts/vclock.py costs` if the host or `SchedulerParameters` change. A2 and A4 decide whether to charge their own costs (SPEC §8) |
| Fair-share has no history at the start of a trace | Warm-up window excluded from metrics (SPEC §9) |
| Policies barely differ on these workloads | P4 gate: stop and retune before any adaptive work |
| Thresholds overfit the evaluation data | P6 calibration seeds are kept separate from P8 evaluation seeds |
| The run matrix is too large to finish | Small cluster model, and B3 resumable runner; run headline comparisons first |
