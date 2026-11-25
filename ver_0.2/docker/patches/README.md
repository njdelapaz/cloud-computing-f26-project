# Patches to the pinned simulator

The Docker build copies the pristine clone `third_party/slurm_simulator` (@ `f99a71e`) and applies every `*.patch` here in name order, before the plugin forks are registered. The clone itself stays unmodified. To change a patch, run `scripts/sim_patch.sh apply <patch>`, edit the clone, then run `scripts/sim_patch.sh save <patch>`, which writes the patch back and restores the clone (`<patch>` defaults to `sim_determinism.patch`; edit one patch at a time). `scripts/sim_patch.sh check` confirms the clone is clean before a build.

| Patch | What it changes |
|---|---|
| `sim_decay_time.patch` | stock `priority/multifactor`: the decay loop's time moves with the simulated clock (below) |
| `sim_determinism.patch` | the virtual clock and the stale completion-event fix (below) |

## `sim_decay_time.patch`

**The bug, in the stock simulator.** The simulator calls the priority plugin's `_decay_thread()` once per `PriorityCalcPeriod` instead of running it as a thread. In simulator builds the function sets its static `start_time = time(NULL)` only on the first call; the line that refreshes it in real Slurm comes after the loop's sleep, which the simulator path returns before. So `start_time` stayed at the run's first second, and:
- usage never decayed (`PriorityDecayHalfLife` had no effect), and running jobs accrued no usage until they ended;
- Fair Tree (Slurm 23.11's default) still recomputed every pending job's priority each tick, but against the frozen time, so **the age factor was always 0**. Every Age weight did nothing, and order came from fair-share alone (plus JobSize), with FIFO for ties.

Found in A2 (`results/a2/decay_probe.txt`): a job that waited 29 minutes kept an age term of 0.

**The fix.** Each call after the first sets `start_time = time(NULL)`, as real Slurm does after each sleep. `plugin/priority_adaptive/priority_adaptive.c` has the same line, so stock and fork still match (G1). It applies in both clock modes.

## `sim_determinism.patch`

Two changes that together make a run depend only on its inputs. Each is described below.

### 1. A virtual clock

**Why.** In the stock simulator, slurmctld's simulated time is real time plus an offset. The event loop jumps the offset forward over idle gaps, but while slurmctld works the clock moves at real speed. So the sub-second moment each event lands at depends on how fast the host happened to run. On a busy day that moves a start by a second here and there, backfill then decides differently, and the difference snowballs: in A1, two identical runs of the probe day differed on 2,110 of 3,028 jobs.

**What changes with `ClockMode = virtual`** (slurmctld only; slurmdbd keeps the stock clock):
- Simulated time is a counter. It moves only when:
  - the event loop skips ahead to the next due event or timer, as before;
  - the main thread sleeps or does a timed wait (backfill's lock yield, for example). Nothing else runs on that thread meanwhile, so the clock just moves to the wake-up time;
  - a fixed cost is charged (below);
  - the event loop makes a pass in which nothing else moved the clock. It then ticks 1 µs, so waits measured in time (such as `sched_min_interval`) end.
- The clock starts at exactly `TimeStart`, however long startup took in real time. So a run's submit times are `TimeStart + SecondsBeforeFirstJob + dt`, with no startup offset. On the real clock it varied by several seconds from run to run.
- The event loop also skips through the final `TimeAfterAllEventsDone` wait. On the real clock that wait ran in real time.
- slurmctld runs no other threads in the simulator: the patch checked this at debug level, and every thread it asks for is either skipped or turned into a simulator event. So with the clock fixed, and the stale-event bug in part 2 fixed, a run depends only on its inputs.

**Fixed costs** (`sim.conf`, in seconds; charged only with `ClockMode = virtual`):

| Key | Charged | Stands in for |
|---|---|---|
| `SchedPassCost` | at the start of each main-scheduler pass | building and sorting the queue |
| `SchedJobCost` | per job the main scheduler tests | one placement attempt |
| `BfCycleCost` | at the start of each backfill cycle | building the queue and the node-space map |
| `BfJobCost` | per job backfill tests | one trial placement |
| `SubmitCost` | per job submission | processing the submit RPC |
| `CompleteCost` | per job-complete or epilog-complete event that ends a job | processing the completion |
| `BackgroundCost` | per `_slurmctld_background` call (once a simulated second) | timers, time limits, state saves |
| `DecayCost` | per priority decay run (every `PriorityCalcPeriod`) | the fair-share recalculation |

Costs per job tested keep the stock behavior that a long pass starts its later jobs later. Backfill still yields its locks after `bf_yield_interval` of charged time. The slurmdbd agent and the database-index calls are not charged: in a real slurmctld they run on their own threads, so only the simulator put them on the scheduling path.

**Measuring the costs.** `CostLog = <file>` writes one line per scheduler pass, backfill cycle, event and background call: `kind,sim_utime,n,real_usec,charged_usec,mono_usec`. Here `n` is the number of jobs tested, `real_usec` is measured with `CLOCK_MONOTONIC` (backfill excludes the time spent yielding), `charged_usec` is what the virtual clock added, and `mono_usec` is the `CLOCK_MONOTONIC` time the line was written, so gaps between lines show real time spent outside the measured work. `scripts/sim_run.sh` always writes it to `log/cost.csv`. `scripts/vclock.py costs` fits the values above from real-clock runs, using least squares for the two scheduler lines and means for the rest, so each fit keeps the real runs' total scheduler time.

**Not covered.** A plugin's own extra work, such as A4's node scoring, costs real time but doesn't move the virtual clock unless it is charged. A plugin can call `sim_charge(usec)` from `contribs/sim/sim_time.h` to charge a fixed cost of its own.

With `ClockMode = real`, the simulator's own default when `sim.conf` doesn't set it, the clock behaves as in the stock simulator. ver_0.2's templates set `ClockMode = virtual` (SPEC §11 #23). The cost log and part 2's fix still apply.

### 2. Completion events carry the job id

**The bug, in the stock simulator.** A job's completion event held a pointer to the job's `sim_job` record. A job that runs past its time limit gets a second completion event when slurmctld kills it, and the record is freed once the job ends. The first event still fired at start + runtime and read the freed memory. If a newer job's record had reused that memory, the event completed the newer job early. Which job that hit depended on the heap layout, so it differed from run to run. Found on the probe day, where `jobid_855` (runtime 9,021 s, limit 9,000 s) was killed at its limit, and 21 s later its stale event ended `jobid_1360` after 15 s instead of 2,601 s. The probe trace has such jobs because `scripts/p1_speed_probe.py` doesn't cap runtimes. B1's converter caps them at the limit, which makes the bug rarer but doesn't rule it out: a job whose runtime equals its limit can still be killed before its own completion event fires.

**The fix.** Completion and epilog events now carry the job id, and an event whose job is no longer active is ignored (logged at `debug`). This applies to both clock modes.
