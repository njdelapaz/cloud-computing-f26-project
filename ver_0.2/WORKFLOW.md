# Workflow (proposed)

How to get from `SPEC.md` to results. Phases are **gated**: each ends with an artifact plus a command that reproduces it, and the next phase starts only once that gate passes.

## Principles
1. **Scope:** all work stays inside `ver_0.2/`.
2. **Spec first:** `SPEC.md` holds the what and why, and changes only by an explicit decision, which gets logged in `PROGRESS.md`.
3. **Freeze interfaces early:** config formats, trace format, decision-log schema and metric definitions are settled before the C and Python work splits into two tracks.
4. **Validate pieces before combining them:**
   - the plugin must match stock multifactor before it adapts
   - policies must do what their names say in static runs before adaptive runs
   - thresholds are calibrated on seeds kept separate from evaluation seeds
5. **Reproducible:** every experiment is one command from config to plots, and results are never edited by hand.
6. **Tracking:** `PROGRESS.md` records status, decisions, results and a change log, with no time estimates.

## Phase map
```
P0 Spec ─► P1 Simulator env ─► P2 Interfaces ─┬─► Track A: plugin  (A1 ─► A2 ─► A3) ─┬─► P4 Static policies ─► P5 Switch overhead ─► P6 State calibration ─► P7 T_min ─► P8 Evaluation ─► P9 Report
                                              └─► Track B: tools   (B1 ─► B2 ─► B3) ─┘
```
Tracks A and B touch disjoint folders (`plugin/` vs `workloads/`, `analysis/`, `experiments/`), so they can run in parallel.

## Phases and gates

| Phase | Work | Gate |
|---|---|---|
| **P0 Spec** | Answer SPEC §11 open questions; merge `SPEC.proposed.md` into `SPEC.md`; create `PROGRESS.md` | No open question left that blocks P1–P2 |
| **P1 Simulator env** | Docker build of the newer UB simulator (Slurm 23.x) in `docker/`; record the exact Slurm release. Tiny cluster config and a hand-written ~10-job trace. Run the same trace 3× to check determinism. Find which clocks the simulator virtualizes, and confirm it tracks memory as a consumable resource (`CR_Core_Memory`) | One script runs the smoke trace. A hand edit to `PriorityWeight*` in `slurm.conf` changes start order exactly as predicted. Clock and memory-tracking findings recorded |
| **P2 Interfaces** | Example `policies.conf`, `schedule.conf`, `adaptive.conf`, neutral trace schema, decision-log CSV schema, metric definitions | Python validators accept every example file. Schemas are recorded in SPEC |
| **A1 Plugin: static** | Fork `priority/multifactor` → `priority/adaptive` that loads one policy from `policies.conf`, and add the balance factor (SPEC §6.1) | **Identical** start order to stock multifactor on the smoke trace and on a W0 trace, with balance weight 0 (G1). Balance factor matches hand-computed values on synthetic snapshots |
| **A2 Plugin: switching** | Hot swap, schedule/override, cooldown, decision log. `decide()` as a pure function | Unit-test table passes, covering override boundaries, cooldown skip with no sampling, and override-vs-cooldown rule. A scripted-window trace switches at the expected sim times (G2) |
| **A3 Plugin: states** | Snapshot, the four signals, hysteresis, severity ranking → policy | Unit tests on synthetic snapshots pick the expected policy for each case |
| **B1 Workloads** | Trace filter, random-job sampler and simulator converter (SPEC §9.2). Episode finder for W1, W2, W4 and W6, with onset sidecars (§9.3) | Filtered-trace job and core-hour counts match SPEC §9.2. Each sample's offered load is within target. Episode list recorded in PROGRESS |
| **B2 Metrics** | Metrics from completion logs and decision log | Metrics on a tiny hand-computed trace match the hand calculation |
| **B3 Runner** | Run-matrix config → simulator runs → `results/<run-id>/`. Resumable, records git state and seed | One command runs a 2×2 mini-matrix end to end |
| **P4 Static policies** | Tune the 3 component vectors, derive the 10 policies, run static × W0–W6 × calibration seeds | **G5:** each single-metric policy leads on its own metric. If not, retune and repeat. Final weights go in the PROGRESS decisions log |
| **P5 Switch overhead** | Forced F↔U alternation at each period; swap/sample cost vs pending count; churn | Overhead plots plus a cost-of-switching curve. X and Y for the T_min rule chosen from this data and written into SPEC §8 |
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
| The simulator's Slurm version has a different plugin API than current docs | P1: build a trivial no-op priority plugin first |
| Time virtualization makes `swap_us` meaningless | P1 clock check; fall back to an offline microbenchmark of the recompute pass |
| Simulator runs aren't deterministic | P1 triple run; use more seeds if variance is high |
| Fair-share has no history at the start of a trace | Warm-up window excluded from metrics (SPEC §9) |
| Policies barely differ on these workloads | P4 gate: stop and retune before any adaptive work |
| Thresholds overfit the evaluation data | P6 calibration seeds are kept separate from P8 evaluation seeds |
| The run matrix is too large to finish | Small cluster model, and B3 resumable runner; run headline comparisons first |
