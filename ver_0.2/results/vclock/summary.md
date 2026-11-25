# Option B trial: a deterministic simulator

The user chose to try option B for A1's run-to-run spread, then adopted it once the gate passed: patch the simulator so identical inputs give identical runs, and charge slurmctld's work a fixed simulated cost. Gate: `scripts/check_vclock.sh` (output in `check_vclock.txt`). Fitted costs: `costs.txt`. Patch notes: `docker/patches/README.md`. Host: AMD Ryzen 9 3950X; image `ver02/slurm-sim:vclock`.

## What was built

| File | What it does |
|---|---|
| `docker/patches/sim_determinism.patch` | applied to the pristine clone at image build (`docker/Dockerfile`). (1) `ClockMode = virtual` in `sim.conf`: slurmctld's clock moves only on skips, main-thread sleeps and fixed costs. (2) A fix for a stock bug: stale completion events completed the wrong job |
| `scripts/sim_patch.sh` | `apply` / `save` / `check`: edit the patch without leaving `third_party/` dirty |
| `scripts/sim_run.sh` | `SIM_SET="Key=Value ..."` overrides `sim.conf`; every run writes `log/cost.csv` |
| `scripts/vclock.py` | `costs` (fit the fixed costs from real-clock runs), `compare`, `gate`, `scale` |
| `scripts/check_vclock.sh` | the gate below; `SENS=1` adds the cost-sensitivity runs |
| `config/{smoke,project}/sim.conf.in` | the fitted costs and `ClockMode = virtual`, adopted as the default after this trial |

## Two causes of the spread, not one

1. **The clock.** As A1 found, simulated time runs at real speed while slurmctld works, so where each event lands within a second depends on host speed, and the differences snowball.
2. **A stock simulator bug, found here.** A job's completion event held a pointer to its `sim_job` record. Every job gets a second completion event, because the simulator treats the terminate request a finished job sends as a kill, and a job killed at its time limit gets one too. Once the job ended, its record was freed, and the stale event read freed memory. Usually it read garbage: one real-clock run logged 3,027 `Can not find record for 1412772141 job!` errors, about one per job. But when a newer job's record had reused the memory, the stale event completed that job early. Example: `jobid_855` (runtime 9,021 s, limit 9,000 s) was killed at its limit. 21 s later its stale event ended `jobid_1360` after 15 s instead of 2,601 s. Which job got hit depended on heap layout, so it changed from run to run. Fixed by passing the job id and ignoring events whose job has ended.

Every job's runtime should be its trace runtime, or its time limit when the runtime is longer (killed within a minute of the limit). On the probe day (3,028 jobs):

| Runs | Ended early | Ended more than 2 s late | Ended 1–2 s late |
|---|---|---|---|
| virtual clock, fixed | 0 | 0 | 0 |
| real clock, fixed (4 runs) | 0 | 0 | 0–71: the event loop was busy when the completion fell due, a real-clock effect |
| virtual clock, before the fix (2 runs) | 5 and 8 | 1 and 2 | 0 |
| real clock, before the fix (4 runs) | 0–4 | 1–8 | 0–76 |

A job that ends far too late lost its own completion event to the bug and ran on to its time limit. For example, `jobid_1326` ran 7,200 s instead of 3,450 s.

## Fitted costs

From six real-clock runs on the fixed image: the probe day four times, plus the B1 samples 2024-01-30 seed 2 and 2024-02-12 seed 3. Each cost is measured with `CLOCK_MONOTONIC` around the work itself. Least squares for the two scheduler lines and means for the rest, so the total simulated time charged equals the real runs' total.

| Key | Value | Basis |
|---|---|---|
| `SchedPassCost` + `SchedJobCost` | 155 µs + 9.5 µs per job tested | 86,337 passes, R² 0.84 |
| `BfCycleCost` + `BfJobCost` | 0 + 421 µs per job tested | 15,243 cycles, R² 0.67. The intercept came out negative, so the line goes through 0. Per job it ranges from 278 µs (fewer than 10 jobs tested) to 445 µs (200–600 tested) |
| `SubmitCost` | 997 µs | 29,151 submits |
| `CompleteCost` | 130 µs | 29,151 completions. The 29,151 duplicate events the fix now ignores cost 28 µs each and are not charged |
| `BackgroundCost` | 291 µs | per `_slurmctld_background` call (once a simulated second); median 1 µs, with rare calls up to 132 ms |
| `DecayCost` | 7.6 ms | per decay run |

Not charged: the slurmdbd agent (mean 10.4 ms per call) and the database-index calls (2.9 ms). In a real slurmctld they run on their own threads; only the simulator puts them on the scheduling path. They dominate real time: in a probe-day diagnostic, slurmdbd round trips took 89% of the run's real time.

## Gate (virtual clock, fitted costs)

All 4 gating checks pass; see `check_vclock.txt`.

| Check | Result |
|---|---|
| Two identical runs are byte-identical | smoke 7 jobs, contended 41, probe day 3,028, 2024-01-30 seed 2 6,092, 2024-02-12 seed 3 10,947 |
| Adaptive BASE == stock BASE (start, end, nodes) | 0 differences on smoke, contended and the probe day |
| JobSize-only control differs | smoke 4 of 7 jobs, contended 39 of 41 |
| Costs set and charged | yes |

## Fidelity: the probe day on both clocks (waits in s)

| Run | mean | median | p95 | >60 s |
|---|---|---|---|---|
| virtual clock, fitted costs | 847 | 2 | 2,137 | 911 |
| virtual, every cost halved | 876 | 1 | 2,346 | 962 |
| virtual, every cost doubled | 874 | 2 | 2,220 | 885 |
| real clock, fixed image (4 runs) | 831–902 | 2 | 2,048–3,357 | 845–891 |
| real clock, A1 (2 stock runs, before the fix) | 1,099 and 5,727 | 1 and 2 | 4,589 and 49,941 | 930 and 1,175 |

- The virtual run sits inside the real-clock range on mean, median and p95, and 2% above it on jobs waiting more than 60 s.
- With the bug fixed, the real clock's aggregate spread shrinks from A1's 5× to about ±4% in mean wait. Job by job it is still large: any two real-clock runs differ on 1,751–2,319 of 3,028 jobs. On the virtual clock they differ on none.
- The costs matter for individual jobs, not much for aggregates. Halving or doubling every cost changes 2,339 jobs but moves the mean wait by about 3%.
- Real time: about 700 s per probe day on the virtual clock against about 745 s on the real clock. Both are dominated by slurmdbd round trips.

## Not covered
- **Plugin overhead.** A plugin's own extra work (A4's node scoring, A2's switch pass) costs real time but doesn't move the virtual clock, unless the plugin charges a fixed cost through `sim_charge()`. So under the virtual clock, SPEC §8's claim that the scorer's real CPU cost shows up in simulated time no longer holds.
- **Host dependence.** The costs were fitted on this host. Another host would fit different values, but with fixed values in `sim.conf`, the results no longer depend on the host.
- **The real-clock reference.** Real-clock runs used for fidelity include slurmdbd latency in simulated time, which the virtual clock leaves out on purpose.
