# PROGRESS — ver_0.3

Status tracker for [PLAN.md](PLAN.md). It records status, decisions and results only; no task
lengths or dates beyond the change log.

**Current phase:** 1 — Workloads, classes, weight configurations (Steps 1–3)

## Phase tracker

| Task | Status | Notes |
|---|---|---|
| 1.1 Folder setup and tooling | done | Layout, `.venv`, pinned `requirements.txt`, `pytest.ini`, layout tests (3 pass) |
| 1.2 Simulator bring-up | done | Image `slurmsim:v03` built; smoke run: all 36 jobs as specified; probe: no CLI or reconfigure mid-run (`sim/README.md`) |
| 1.3 Trace profiling | done | All 8 data checks pass; K = 2024-01-29 week, N = 2024-05-20 week; outputs in `results/profile/` |
| 1.4 Job classifier | done | 22 tests pass; January shares reproduce SPEC §4.3 exactly; thresholds frozen |
| 1.5 Workload builder | done | K, N, X × seeds 1–3 built and deterministic; K/N memory load 0.947–0.956; 54 tests pass (`results/workloads/`) |
| 1.6 Weight configurations | done | B0, C1–C6 rendered from measured scales; fair-share scale provisional (0.3) until 1.7; 73 tests pass |
| 1.7 Configuration check in the simulator | not started | |
| 2.1 Specify Steps 4–5 | not started | waits for Phase 1 |
| 2.2 Determine trigger states | not started | |
| 2.3 Build the change mechanism | not started | |
| 3.1 Specify Step 6 | not started | waits for Phase 2 |
| 3.2 Run the simulations | not started | |
| 3.3 Scale up | not started | |
| 3.4 Analyze and report | not started | |

## Pinned third-party sources

| Checkout (in `third_party/`) | Branch | Commit |
|---|---|---|
| `slurm_simulator` (UB Slurm Simulator; binary reports Slurm 23.11.0-0rc1) | `slurm-23-02-sim` | `f99a71eb2073266bba3869aa2a0ccd51c58c9bb7` |
| `slurm_sim_tools` (runner and tools, v3.0) | `v3.0-branch` | `21ba58d5b88b2a208a4b2db8b2b31c86d41a3847` |

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Folder names | `third_party/`, `workloads/` (`cache/`, `traces/`), `results/<group>/<run>/` | Match the committed `ver_0.3/.gitignore`. |
| Simulator image | Own lean image from `sim/Dockerfile` (`slurmsim:v03`) built from the pinned checkouts | The upstream image adds Jupyter, RStudio, Eclipse and Chrome, which the simulation does not need. |
| Python | 3.12 venv in `.venv/`, pinned `requirements.txt` | SPEC conventions. |
| Class thresholds | Kept as proposed: size 1/16 and 1/4 of a node; time 4 h and 24 h | All nine classes are populated in every month (`results/profile/classes_by_month.csv`). |
| Calibration week `K` | 2024-01-29 to 2024-02-04 (week 4) | Second most typical eligible week, at least two weeks from `N`. |
| Evaluation week `N` | 2024-05-20 to 2024-05-26 (week 20) | Most typical of 22 eligible weeks (`results/profile/selection.json`). |
| Ineligible weeks | Jan 1 week; Mar 11–17 (maintenance Mar 12); Jun 17 onward | Holiday; jobs killed by maintenance; monthly files hold jobs by end time, so the last two weeks lose jobs ending after June 30. |
| Mid-run control (input to 2.1) | Weight changes must happen inside the simulator's event loop (a simulator patch); an outside controller using `scontrol reconfigure` is not possible | The simulator never starts `slurmctld`'s RPC thread: every mid-run `squeue`, `sprio`, `sshare`, `scontrol` call fails (`results/probe/probe.json`). |
| Priority checks | `DebugFlags=Priority` lines in `slurmctld.log` replace `sprio` | `sprio` needs the RPC thread; the debug log has every job's factors and weights in simulated time. |
| Thinning | Seeded order, keep the prefix closest to ρ* (Bernoulli with solved p) | Deterministic per seed; hits ρ* within 1% for K and N. |
| Scenario X load | Each segment (and each 6-h block) at ρ* in the resource it saturates; no week-level balancing | Forcing the week's memory total onto one day pushed that day to 1.27×; per-segment targets match SPEC's intent. Week comes out at memory 0.91–0.93. |
| X burst account | `account026` (most memory load in the pool) | Deterministic choice. |
| Job-size direction (SPEC §5.1 corrected) | `PriorityFavorSmall=NO` with `SMALL_RELATIVE_TO_TIME`: shortest time limit first | Slurm 23.11 computes `max(32, cpus)/T_min/512` for single-node jobs, so only the limit matters; `FavorSmall=YES` (the first draft) would have favored long jobs. |
| Weight scaling (SPEC §5.4 corrected) | Gini mean difference of each factor, not P95 − P5 | Over 95% of week-K jobs ask for 1 core, so the CPU factor's P95 − P5 is 0. U–L correlation on K: Pearson −0.006. |
| Scenario Y (added on request) | Natural background at 0.6·ρ* plus 22 random 3–12 h bursts of X's patterns at 0.4·ρ* each; ≥1 job per burst | Tests overlapping, hour-scale changes. Week memory load 0.88–0.96 across seeds; 47k–56k jobs. |
| Week boundaries | Monday 00:00 UTC | The source cluster is on Europe/Paris time, so weeks start at 01:00 or 02:00 local; the monthly files are cut at local midnight. |

## Change log

| Date | Commit | Change |
|---|---|---|
| 2026-11-26 | `677798c` | 1.1: folder layout, Python env, layout tests, this tracker |
| 2026-11-26 | `b250f9d` | 1.4: nine-class job classifier and boundary tests |
| 2026-11-26 | `2dbe58a` | 1.3: trace cache, data checks, weekly profile, weeks K and N |
| 2026-11-26 | `6b17bb7` | 1.2: simulator image, smoke test, mid-run probe |
| 2026-11-26 | `67df735` | 1.5: workload builder; scenarios K, N, X × 3 seeds |
| 2026-11-26 | `f2ed7da` | 1.6: weight configurations B0, C1–C6; SPEC §5 corrections |
