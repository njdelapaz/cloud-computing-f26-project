# PROGRESS — ver_0.3

Status tracker for [PLAN.md](PLAN.md). It records status, decisions and results only; no task
lengths or dates beyond the change log.

**Current phase:** 4, revised strategy (SPEC §9 approved; next task 4.3). Phase 3 complete (3.7 skipped on request); answer to SPEC §1: no ([REPORT.md](REPORT.md))

## Phase tracker

| Task | Status | Notes |
|---|---|---|
| 1.1 Folder setup and tooling | done | Layout, `.venv`, pinned `requirements.txt`, `pytest.ini`, layout tests (3 pass) |
| 1.2 Simulator bring-up | done | Image `slurmsim:v03` built; smoke run: all 36 jobs as specified; probe: no CLI or reconfigure mid-run (`sim/README.md`) |
| 1.3 Trace profiling | done | All 8 data checks pass; K = 2024-01-29 week, N = 2024-05-20 week; outputs in `results/profile/` |
| 1.4 Job classifier | done | 22 tests pass; January shares reproduce SPEC §4.3 exactly; thresholds frozen |
| 1.5 Workload builder | done | K, N, X × seeds 1–3 built and deterministic; K/N memory load 0.947–0.956; 54 tests pass (`results/workloads/`) |
| 1.6 Weight configurations | done | Redesigned 2026-11-26: B0 and C0–C3 (favor none / large / small / medium via one QOS per size class, then shortest time limit, then fairness; strict size) |
| 1.7 Configuration check in the simulator | done | Pass v3c: weights and QOS factors match in all 5 runs; favored class first in 100% of cross-class pairs; time ahead of fairness within classes (`results/check/configs_v3c.json`) |
| 2.1 Specify Steps 4–5 | done | SPEC §6–7 and PLAN 2.2–2.6 approved 2026-11-26 with default answers to Q-A to Q-C |
| 2.2 Trigger calibration | done | K-s1 week under C0 (1 h 19 min; mean wait 2.0 h vs 9.1 h under FIFO). Waiting-queue shares: small median 0.59, medium 0.28, large 0.09. Replay: large 31%, mix 29%, medium 22%, small 18% of time; 4.3 switches/day (`config/triggers.yaml`, `results/phase2/trigger_calibration.json`) |
| 2.3 Policy module | done | `controller/policy.py`, `tick.py`, `replay.py` (standard library only; runs under the container's Python 3.10); 10 tests pass |
| 2.4 Simulator hook | done | `sim/patches/policy_hook.patch` applied at build; image `slurmsim:v03` now includes it; with the hook off it matches the unpatched image (same nodes, start times within the 1 s run-to-run jitter) |
| 2.5 Apply test | done | C0→C1→C2→C3→C0 during a standing queue: every waiting job recomputed within 2 s with the new weights and QOS factors; missing config rejected; failing command logged, no switch (`results/phase2/apply_test.json`) |
| 2.6 Adaptive week | done | Full K-s1 week with policy ticks: 1,981 ticks logged on both sides, 0 command failures, 49/49 switches applied, replay exact, run check passes; submission week: 6.6 switches/day, 28% reversals within 2 h (`results/phase2/K-s1-adaptive_summary.json`) |
| 3.1 Specify Step 6 | done | SPEC §8 and PLAN 3.2–3.8 approved 2026-11-26 with the default answers to E-A to E-E: nine policies (B0, B1, C0–C3, A1–A3), 93 one-week runs |
| 3.2 Evaluation tooling | done | B1 rendered and its static keys applied per run; trigger variants as overlays (`triggers_A2.yaml`, `triggers_A3.yaml`); `config/evaluation.yaml`; `evaluation/metrics.py` (K runs: 119.5 / 84.1 min mean wait, matching Phase 2), `evaluation/compare.py`, `sim/batch.py`; B1 check run passes (`results/phase3/image_checks.json`); 121 tests pass |
| 3.3 Arrivals variant (A3) | done | Hook counts submissions by QOS (image rebuilt; Phase 2 image kept as `slurmsim:v03-phase2`); policy `signal: arrivals`; calibrated on the K-s1 workload; hook-off smoke matches the Phase 2 image; apply test passes again; A3 slice: 770 ticks match sacct, replay exact |
| 3.4 Pilot | done | Six pilot runs pass (after `MaxJobCount=200000` and the known-issue rules below). Per run: N 71 min, 1.1 GB, 155 MB disk; Y 126 min, 2.6 GB, 350 MB; X 177–183 min, 4.1 GB, 540 MB; under one core each. The N-s1 batch of nine took 71–101 min, the same as alone. Batch size: nine at once for every scenario; one X batch at a time (36 GB). Pilot (one seed): A1 mean wait above C0's on N (102 vs 76 min), X (371 vs 324) and Y (48 vs 36) |
| 3.5 One-week evaluation runs | done | 57 runs (45 matrix: B1, C0, A1–A3 × N, X, Y × 3 seeds; 6 pilot; 6 repeat), all pass under the current check rules (`results/phase3/metrics.csv`). Real time per run (median / max): N 73 / 101 min, Y 138 / 196 min, X 181 / 275 min; peak 1.1 / 2.7 / 4.3 GB. Known simulator issues over all 57: 1,940 completions and 319 submissions seconds late, 163 lost finish events (138 on X, ~7 per X run), 2 early finishes. 16 runs of the dropped B0 and C1–C3 kept aside |
| 3.6 Analysis of the one-week results | done | **Answer: no** (SPEC §8.6). `S*` = C0 (142 min mean wait over 9 workloads; B1 280). No adaptive variant effective anywhere: 26 of 27 comparisons worse than C0 (15 beyond the 22–31% noise bands); the one win (A3, N-s2, −25%) fails the small-job guard. Adaptive policies shift waits from medium/large jobs to small ones (~90% of jobs). `results/phase3/comparison.json`, five figures `results/phase3/fig_*.png` |
| 3.7 Scale up (`M`) | skipped | Skipped on request (2026-11-27) after the one-week answer was a clear no; `M` was not built |
| 3.8 Report | done | `REPORT.md`: answer no, with results by scenario, size class and day, the favored-class analysis, the fixed favor-small context, why switching did not pay, limitations and reproduction |
| 4.1 Specify the revised strategy | done | SPEC §9 and PLAN 4.2–4.7 approved 2026-11-27 (requested). B1 baseline; favored medium and large spells only (no small state), with a 5,000-point QOS boost (S-C); trigger: the median wait of a class's waiting jobs reaches its target, the class is at least 10% of the queue and at least 20 jobs wait (S-B, S-G); each 60-min spell is followed by a return to B1 and a stabilization period. One-day setup workloads `D` (7 patterns × 3 seeds, 12 h lead-in, weeks 1–12). 238 runs in two stages: target level (tight / middle / loose), then the stabilization period (0–480 min). B1 is the bar, C0 a reference. Defaults kept for S-A, S-D, S-E, S-F |
| 4.2 One-day setup workloads (`D`) | done | 21 workloads `D-<pattern>-s1..3` built and byte-identical on rebuild; X-s1 and Y-s1 still rebuild byte-identical after the builder refactor. Source weeks 1–9, 11, 12; 12 h lead-in shared by the patterns of a seed (1.4k–2.5k jobs). Measured day: nat 4.4k–5.5k jobs, flood 42.5k–42.9k, lglong 63–99, acct 2.7k–3.0k, mem 2.4k–2.7k, alt 20.8k–21.4k, over 9.7k–10.6k. Loads 0.83–0.95 in the saturated resource for single-pattern days (one large-long job can carry 0.41 of a day's memory), alt 0.84–0.88, over 0.86–1.11 (`results/workloads/D_days.csv`, `D_bursts.csv`). 183 tests pass |
| 4.3 B1-based favored states | pending | |
| 4.4 Return-to-baseline policy (A4) | pending | |
| 4.5 Stage 1: target level | pending | |
| 4.6 Stage 2: stabilization period | pending | |
| 4.7 Analysis of the setup | pending | |

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
| Weight calibration (SPEC §5.4 revised) | Scales = Gini mean difference among *waiting* jobs, measured per configuration on the queue it produces | Pass 1 (all jobs): 1/6 configs in order; pass 2 (pooled waiting jobs): 4/6; pass 3 (per configuration): 6/6. Large jobs pile up in the queue and short-limit jobs leave it, so the waiting queue differs from the workload. |
| Factor formulas verified | TRES = request / partition total; job size = 32/T_min/512 | Computed factors match Slurm's logged values to 5×10⁻⁷. |
| ~~C6 margin~~ | Superseded | The six-configuration set was replaced (see below). |
| Full-week cost | One week of K under FIFO: 2 h 05 min real time (13.6 simulated days incl. drain) | Sets the run budget for Phase 3; runs share the machine. Mean wait 9.1 h, p95 15.1 h, utilization 0.78. |
| Simulator issue | Rare lost finish event: the job runs to its limit | 1 of 26,119 jobs in the full week; reported by `sim/check_run.py` as a known issue (`sim/README.md`). |
| Load balance measure | Measured on cluster state: σ across the 16 nodes of per-node utilization (CPU+mem)/2 | Requested 2026-11-26. Priority weights barely move it (σ 0.199–0.210 on the 36-h checks); Slurm's default placement fills low-numbered nodes first. |
| Configurations redesigned (requested 2026-11-26) | Four configurations for four queue scenarios: C1 favor large (many large waiting), C2 favor small (many small), C3 favor medium (many medium), C0 neutral (mix); B0 FIFO baseline | Replaces the brief's six fairness/utilization/load-balance orderings, and the interim four two-goal pairings. Utilization and load balance are measured on cluster state instead. |
| Size preference mechanism | One QOS per size class (`small`, `medium`, `large`; identical limits); configurations set QOS priorities | Chosen over a site-factor plugin and partitions. Slurm's own size factors are monotonic and cannot favor medium. |
| Ordering | Size class, then shortest time limit, then fairness | Requested 2026-11-26 (time and fairness swapped from the first draft). |
| Strict size first | One size step outweighs the largest possible time + fairness + age total | Requested 2026-11-26. With soft tiers the favored class led only 66–93% of cross-class pairs; the 1/T time factor makes short limits dominate. |
| Time/fairness calibration | Gini mean difference among waiting jobs of the same class, per configuration (from pass v3b) | Within classes is where they decide order; each configuration reshapes its own queue. |
| Run-to-run variation | Two FIFO runs of the same 36-h slice: mean wait 88 vs 51 min | Single runs show direction only; Step 6 needs several runs per configuration. |
| Trigger signal (SPEC §6) | Each size class's share of waiting jobs, as a percentile of its own range in a C0 run of K; enter at P80, exit below P60, 2-tick persistence, 60-min dwell, 10-min ticks | Counted by job, small is 74–94% of the waiting queue and large 0–5% (FIFO K week). A fixed share would always pick small; a ratio to the median would fire for large on 1% → 2% and never for small. |
| Change mechanism (SPEC §7) | A patch to the simulator adds a policy tick event that writes a snapshot, runs a Python policy, and applies weights (priority plugin reconfigure) and QOS priorities (accounting manager update) in place | No RPCs mid-run (task 1.2), so control must sit inside the event loop. Both apply paths exist in the pinned Slurm source. |
| Step 4 open questions (approved 2026-11-26) | Q-A waiting queue; Q-B count jobs; Q-C enter P80 / exit P60, 60-min dwell | Defaults approved with the Step 4–5 spec. |
| Trigger calibration source | Neutral C0 run of K, not FIFO | The waiting queue's class mix depends on the configuration: under C0 short jobs start fast, so small falls from 0.90 (FIFO) to 0.59 of waiting jobs. |
| Trigger feedback (finding, task 2.6) | The waiting-queue trigger flips more than predicted: 6.6 switches/day vs 4.3 from the neutral replay; 28% reversals within 2 h; 21 of 45 gaps at the 60-min minimum | Favoring a class drains it from the queue (SPEC §6.5). Mechanism is correct; the trigger signal (Q-A: arrivals instead of the waiting queue) or longer dwell/persistence are the levers for Phase 3. |
| Minimum stay per state (requested 2026-11-26) | `dwell_s` in `config/triggers.yaml` may be one value or a table per state; the default stays 60 min for every state | In the adaptive week small and medium left at the 60-min minimum 8 of 16 and 8 of 9 times; large only 2 of 9. Open-loop replay: 120 min for small and medium, 60 for large and mix, gives 4.9 switches/day vs 6.6 (SPEC §6.5). No simulator change. |
| Step 6 open questions (approved 2026-11-26) | E-A mean wait as the primary metric; E-B run A1, A2 and A3; E-C bar `S*`, the best single static setting; E-D guards +25% size-class wait, −1 point utilization, −0.05 Jain; E-E three runs of C0 and A1 on seed 1 of each scenario | Defaults approved with the Step 6 spec (SPEC §8.9). |
| Job limit (finding, task 3.4) | `MaxJobCount=200000` in `slurm.conf.in` | Slurm's default 10,000 rejected 8,739–9,493 submissions on X's flood day; the simulator then never ended (rejected jobs stay in its active list). Runs from before the change are set aside, so every Phase 3 run has the same settings. Phase 1–2 runs never reached the limit. |
| Startup race (finding, task 3.5) | `sim/batch.py` starts containers 20 s apart and stops and retries a run whose controller refuses a submission in its first 15 min | With ~27 runs starting together, N-s3 A2 (and the dropped N-s2 C3) started slurmctld before the accounts were loaded: all 19,836 submissions were refused and the run never ended. The simulator cannot take account updates after startup. N-s3 A2 is rerun. |
| Fixed settings compared (requested 2026-11-27) | Only C0 and B1; B0 and C1–C3 dropped from the comparison (5 policies × 9 workloads = 45 matrix runs, 57 in all) | C1–C3 are the states the adaptive policies switch to; C0 and B1 are the fixed choices a site would weigh. Runs of the dropped settings already made (N, Y-s1) are kept but not compared. Noted: on N-s1, C2 fixed had the lowest mean wait (65 min vs C0 75, adaptive 88–111). |
| Favored-class effect (finding, task 3.6) | Reported in SPEC §8.6 (`evaluation/favored.py`) | While a class is favored, its jobs wait much less for medium (−55 to −91% on N and Y) and for large on N (−30 to −83%), but never less for small (+17 to +368%): C0 already starts short jobs first, and the small state follows backlogs the policy built. The other classes' extra waiting usually exceeds the favored class's gain. |
| Scale-up skipped (requested 2026-11-27) | Task 3.7 not run; the report rests on the one-week results | The one-week answer is no on every scenario; a four-week run (9–12 runs, 6+ h) would test whether it holds over a month but could not reverse the one-week verdict. Listed as a limitation in REPORT.md. |
| Repeat-run shift (finding, task 3.6) | Noise bands kept as specified (0.22 N, 0.31 X, 0.24 Y) | In all six repeat sets the third run (made 15:44–19:00 UTC alongside X-s3) came out higher than the other two, by up to 31% on X. The count of runs sharing the host does not explain it. The bands are therefore conservative; the verdict does not depend on them (every adaptive comparison but one is worse than C0 or breaks a guard). |
| Early finishes (finding, task 3.4) | Up to 5 jobs per run that the simulator completes early count as a known simulator issue, not a run failure (`sim/check_run.py`, `EARLY_FINISH_MAX`) | Rare simulator glitch: a job completes at another job's completion instant, hours early (1–3 jobs of ~55k in some runs; a repeat usually has none). Failing such runs would cost reruns and could exclude runs for no effect on the metrics; more than 5 still fails. |
| Late completions (finding, task 3.4) | Completions and submissions up to 60 s late count as a known simulator issue (`LATE_TOLERANCE_S`; 10 s at first) | At instants where many jobs end together the simulator processes some completions late: 1 s in every run, 2–5 s for 93 jobs in the X-s1 C0 pilot (19k-job queue), 11 s for one job in N-s3 C3. No effect on minute-scale waits; a lost finish event costs hours, so later than 60 s still fails. |
| Pilot runs together (deviation, task 3.4) | The six pilot runs run at once, not one at a time | Each simulation uses under one core of 32 and about 1–2.6 GB, and the N-s1 runs took 69–73 min both alone-ish and in the batch of nine; one at a time would take most of a day. |
| A3 arrival shares (finding, task 3.3) | Large is 0 in most 60-min windows (1–5 large jobs an hour), so one or two large arrivals put it near P100; replay over K: large 36% of the time, 7.9 switches/day | Kept as approved (risk R10: reported, not tuned). The first real comparison of A3 with A1 comes from the pilot and matrix runs. |
| Arrivals snapshot (task 3.3) | The hook writes running submission totals per QOS (`submitted`); the controller takes the difference over its window | Finished jobs leave the job list after `MinJobAge` = 300 s. Keeps the window a policy parameter, not a simulator setting. |
| Typical-site reference B1 (requested 2026-11-26) | SchedMD's multifactor configuration example, as published: fair share 10000, age 1000, job size 1000 (larger requests first), 14-day half-life and maximum age; also a candidate for `S*` | Slurm's own default (all weights 0) is FIFO, already B0. None of C0–C3 is what a site typically runs, so B1 tests the adaptive policy against a realistic fixed setting. |
| Revised strategy (requested 2026-11-27) | Phase 4 (SPEC §9): B1 is the baseline; only medium and large are favored; each favored spell lasts a fixed time, then B1 holds for a stabilization period set by evaluation; the trigger reads the waiting queue only; setup on one-day workloads, tests later on five-day workloads | Phase 3: favoring small never helped small jobs, favoring medium and large helped their own jobs, and states chained into each other through the backlogs they left. |
| Phase 4 trigger (S-B, chosen 2026-11-27) | Wait targets: a class is favored when the median wait of its waiting jobs reaches its target, provided the class is at least 10% of the waiting jobs and at least 20 jobs are waiting in all (S-G: a share of the queue, requested, in place of a fixed count); three target levels tried on the setup days (2 / 6 h, 4 / 12 h, 8 / 24 h for medium / large) | Asks whether a class has waited too long, which is what Phase 4 judges, instead of whether the queue's mix is unusual; a trigger calibrated on B1 days would count B1's large-job backlog as normal. No calibration run is needed. |
| Phase 4 boost strength (S-C, chosen 2026-11-27) | In between strict and soft: the favored class's QOS is raised by `PriorityWeightQOS = 5000` points (others 0, in both states) | About three typical fair-share gaps under B1 (0.17 × 10000 ≈ 1,700 points), so a favored job comes first except against accounts far less used than its own; strict (> 12,000) would ignore fairness, soft (~2,000) would barely move the order. |
| One lead-in per seed (task 4.2) | The 12 h lead-in depends on the seed only, not the pattern | All patterns of a seed then start from the same cluster state, so differences between patterns come from the measured day. |
| Coarse day loads (finding, task 4.2) | Kept as built: single-pattern days land at 0.83–0.95 of `ρ*`, not within 2% as the weeks do | Run time counts in full on the day a job is submitted, so one large-long job can carry up to 0.41 of a day's memory, and thinning keeps the prefix closest to target (`D-nat-s1`: 0.83, or 1.14 with the next job). Skipping big jobs to hit the target would bias the mix against them. |
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
| 2026-11-26 | `c319cc4` | Scenario Y (overlapping bursts) |
| 2026-11-26 | `318cb1d` | 1.7: configuration check (six-configuration design, since superseded) |
| 2026-11-26 | `614d9e6` | Priority configurations redesigned around size-class QOS; strict size first |
| 2026-11-26 | `ef50823` | 2.1: Step 4–5 spec (SPEC §6–7) and Phase 2 tasks |
| 2026-11-26 | `c9430e7` | 2.3: policy module, tick command, replay |
| 2026-11-26 | `c235b6d` | 2.4: simulator policy hook (patch, image, `--policy` run setup) |
| 2026-11-26 | `ff274cc` | 2.5: apply test of the policy hook |
| 2026-11-26 | `5c5e520` | 2.2: trigger calibration on the neutral K week |
| 2026-11-26 | `5de3e8f` | 2.6: adaptive K week with the queue-state policy |
| 2026-11-26 | `836cd4a` | Per-state minimum stay for the queue-state policy |
| 2026-11-26 | `2a68d08` | 3.1: Step 6 spec (SPEC §8) and Phase 3 tasks |
| 2026-11-26 | `5d51f28` | 3.2-3.3: evaluation tooling and the arrivals variant (A3) |
| 2026-11-27 | `114e8a0` | 3.4 fix: raise MaxJobCount; keep failed attempts; run timeout |
| 2026-11-27 | `d2e22e9` | 3.4: rare early finishes as a known simulator issue; batch --recheck |
| 2026-11-27 | `37963e5` | 3.4: completions a few seconds late as a known simulator issue |
| 2026-11-27 | `4fc60d1` | 3.4: pilot done; submissions a few seconds late as a known issue |
| 2026-11-27 | `352dd06` | Step 6: compare only C0 and B1 as fixed settings (requested) |
| 2026-11-27 | `0c70e54` | 3.5: allow completions and submissions up to 60 s late (was 10 s) |
| 2026-11-27 | `beea9b8` | 3.5: space out container starts; stop runs whose controller refuses jobs |
| 2026-11-27 | `da741fe` | 3.5: a job killed at its limit a few seconds late is a known issue too |
| 2026-11-27 | `dc32182` | 3.5: one-week evaluation runs done |
| 2026-11-27 | `3c12587` | 3.6: analysis of the one-week results; answer is no |
| 2026-11-27 | `2941ce0` | 3.6: effect on the favored class while it is favored |
| 2026-11-27 | `19decca` | 3.8: REPORT.md; Phase 3 complete (3.7 skipped on request) |
| 2026-11-27 | `79da032` | 4.1: revised strategy spec (SPEC §9) and Phase 4 tasks |
