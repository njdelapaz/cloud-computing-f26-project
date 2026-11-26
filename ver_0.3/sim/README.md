# Simulator (UB Slurm Simulator)

Everything here runs from `ver_0.3/`.

## Pinned sources and image

| Item | Value |
|---|---|
| `third_party/slurm_simulator` | branch `slurm-23-02-sim`, commit `f99a71eb2073266bba3869aa2a0ccd51c58c9bb7` |
| `third_party/slurm_sim_tools` | branch `v3.0-branch`, commit `21ba58d5b88b2a208a4b2db8b2b31c86d41a3847` |
| Slurm version (from `slurmctld.log`) | `23.11.0-0rc1` (the branch name says 23-02; the binary reports 23.11.0rc1) |
| Image | `slurmsim:v03`, id `sha256:03f8899bf9eb4f6c11aa5b40cd08ded2e2e4b61bcd89288e5c55f51643a592aa` (with the policy hook, since task 2.4; the Phase 1 runs used `sha256:daeb30c6…`, the same build without the patch) |
| Image contents | Ubuntu 22.04, MariaDB, Python 3.10 with the `slurmsim` runner, Slurm built with `--enable-simulator` |
| Patch | `sim/patches/policy_hook.patch` (policy hook, SPEC §7), applied to the pinned source during the build |

Fetch the sources (gitignored) and build the image:

```bash
git clone --single-branch --branch slurm-23-02-sim https://github.com/ubccr-slurm-simulator/slurm_simulator.git third_party/slurm_simulator
git -C third_party/slurm_simulator checkout f99a71eb2073266bba3869aa2a0ccd51c58c9bb7
git clone --single-branch --branch v3.0-branch https://github.com/ubccr-slurm-simulator/slurm_sim_tools.git third_party/slurm_sim_tools
git -C third_party/slurm_sim_tools checkout 21ba58d5b88b2a208a4b2db8b2b31c86d41a3847
docker build -f sim/Dockerfile -t slurmsim:v03 .
```

`sim/Dockerfile.dockerignore` keeps the build context to the two checkouts and
`sim/entrypoint.sh`, so the dataset is never sent to Docker.

## Running a simulation

1. **Prepare a run directory** from an events file, an accounts file and a priority file:

   ```bash
   .venv/bin/python -m sim.prepare_run results/smoke/run1 --events sim/smoke/events.txt --accounts sim/smoke/accounts.txt --priority config/priority/B0.conf --time-start 1716163200
   ```

   This renders `sim/etc/*.in` into `RUN_DIR/etc/` and writes `users.sim` and
   `sacctmgr.script` from the accounts file (equal shares, SPEC §2.3). `--extra-conf LINE`
   appends lines to `slurm.conf`; use `--extra-conf DebugFlags=Priority` to log priority
   factors.

2. **Run it** (the second argument is `dtstart`, the delay before the first job):

   ```bash
   sim/run_sim.sh results/smoke/run1 60
   ```

   The runner's outputs go to `RUN_DIR/out/`: `slurm_acct.out` (sacct), `slurmctld.log`,
   `jobcomp.log` and others. Its console output goes to `RUN_DIR/run.log`.

3. **Check that every job ran as specified**:

   ```bash
   .venv/bin/python -m sim.check_run results/smoke/run1
   ```

### Input formats

- `events.txt`: one job per line, numbered from `jobid_1001` in submission order so the
  names line up with Slurm's job ids (`FirstJobId=1001`):

  ```
  -dt <submit_s> -e submit_batch_job | -J jobid_<id> -sim-walltime <runtime_s> --uid=<user> -t <limit_min> -n <cpus> --ntasks-per-node=<cpus> -N 1 --mem=<mem_mb> -A <account> -p normal -q normal pseudo.job
  ```

- `accounts.txt`: one `account<TAB>user` association per line.

## Smoke test (task 1.2)

`sim/smoke/` has 36 jobs from 3 accounts and 5 users, one of whom uses two accounts. Sixteen
full-node jobs fill the cluster first, so the rest queue. The set includes memory-bound and
CPU-bound jobs and a job whose run time equals its limit.

Result: every job ran once on one node of `n01`–`n16`, with the requested CPUs, memory, limit,
user and account, the expected run time and the submitted offsets. 31 simulated minutes took
28 s (67×). The job whose run time equals its limit ends `COMPLETED` at the limit, not
`TIMEOUT`.

## Mid-run probe (task 1.2): no RPCs while the simulation runs

`sim/probe.py` keeps all 16 nodes busy for hours of simulated time while 60 jobs wait. It
tries the Slurm CLI inside the container until the run ends. Evidence: `results/probe/probe.json`.

| Question | Answer |
|---|---|
| Q1: Do `squeue`, `sprio`, `sshare`, `scontrol show node` answer mid-run? | **No.** Every call fails with "Unable to contact slurm controller (connect failure)". |
| Q2: Does `scontrol reconfigure` work mid-run? | **No**, same failure. Weights cannot be changed from outside during a run. |
| Q3: Does the run survive those attempts? | Yes. All 76 jobs ran as specified (6 h of simulated time in 37 s, 580×). |
| Q4: Can an outside process read simulated time? | **No.** Simulated time lives inside `slurmctld`: a clock in shared memory that jumps ahead to the next event. |

**Why:** the simulator never starts `slurmctld`'s RPC thread. In
`third_party/slurm_simulator/contribs/sim/slurmctld_controller.c` (line 67), the thread
create for `&slurmctld_config.thread_id_rpc` returns without starting it. The controller is
a single event loop, and priority recalculation runs as a `SIM_PRIORITY_DECAY` event in that
loop.

**Consequence for Step 5 (input to task 2.1):** a weight change, and any reading of cluster
state, has to happen inside the simulator's event loop. That means a simulator patch, for
example a new event that loads `priority_weights.conf` at a simulated time, or a hook at
priority recalculation. An outside controller using `scontrol reconfigure` is not possible in
this simulator unless the RPC thread is re-enabled.

## Policy hook (SPEC §7, tasks 2.4–2.5)

`sim/patches/policy_hook.patch` adds to the simulator:
- a cyclic `SIM_POLICY_TICK` event, which writes `policy/state.json` (waiting jobs and
  per-node allocation), runs `PolicyCommand` and applies the configuration it returns;
- a `-dt <s> -e set_priority_config | <id>` event, for fixed-time switches;
- an apply step. It validates `policy/configs/<id>.{conf,qos}`, sets the QOS priorities
  through the accounting manager, sets the `PriorityWeight*` values and reloads the priority
  plugin, then recomputes every waiting job's priority at once.

`sim.conf` settings: `PolicyDir`, `InitialConfig`, `PolicyTickSeconds` and `PolicyCommand`.
`sim/prepare_run.py` writes them:
- `--policy scheduled`: fixed-time switches only;
- `--policy adaptive`: policy ticks, with `params.json` from `config/triggers.yaml` and a
  `tick.sh` that runs `controller.tick`.

Without `PolicyDir`, nothing changes. A smoke run on the patched image matched the unpatched
one: same nodes, and start times within the 1 s jitter between repeat runs.

**Apply test** (`sim/apply_test.py`, `results/phase2/apply_test.json`):
- Scheduled switches C0 → C1 → C2 → C3 → C0 during K's first standing queue each recomputed
  every waiting job (113, 112, 102 and 20 jobs) within 2 simulated seconds.
- The logged weights and every job's QOS factor matched the new configuration.
- A switch to a missing configuration (C9) was rejected and C1 kept.
- A run whose policy command always fails logged every failed tick, never switched, and ran
  every job as specified.

## Known simulator issue: rare lost finish event

Rarely, the simulator processes a stray "batch script complete" request for a job while it is
still pending. It logs `error: Can not stop <id> job, it is not running (PENDING)` and drops
the job from its active-job list. When the job later starts, it logs
`Sim:sim_insert_event_comp_job: Can not find job <id> among active sim jobs!` and never
schedules the job's finish, so the job runs to its time limit and ends `TIMEOUT`.

Seen once in about 30 runs: 1 of 26,119 jobs in the full FIFO week of `K-s1`, and none in the
36-hour checks. `sim/check_run.py` reports such jobs as a known simulator issue instead of a
failure.

## Full-week reference run (FIFO, `K-s1`)

| Item | Value |
|---|---|
| Jobs | 26,119 |
| Simulated span | 13.6 days (the 7-day week plus the drain) |
| Real time | 2 h 05 min (about 157× on average; slower late in the week as the queue grows) |
| Mean / p95 wait | 9.1 h / 15.1 h |
| Utilization over the week | 0.78 (CPU 0.67, memory 0.89) |
| Node imbalance σ / max−min | 0.16 / 0.54 |

## Other tools

| Command | What it does |
|---|---|
| `.venv/bin/python -m sim.runstats RUN_DIR` | Hourly pending jobs and allocated CPU/memory fractions, mean and p95 wait, makespan, from `out/slurm_acct.out` |
| `.venv/bin/python -m sim.weights calibrate` / `render` | Measure factor scales on scenario K; render `config/priority/*.conf` |
| `.venv/bin/python -m sim.check_configs --hours H --tag T` | Task 1.7: run B0 and every configuration (C0–C3) in parallel on the first H hours of K with priority logging, and check weights, QOS factors, size-first order and within-class order (SPEC §5.5) |
| `sim/priority_log.py` | Parser for the priority debug log (hourly queue snapshots) |
| `.venv/bin/python -m controller.calibrate RUN_DIR` | Task 2.2: trigger percentiles from a neutral (C0) run, written to `config/triggers.yaml` |
| `.venv/bin/python -m sim.apply_test` | Task 2.5: scheduled switches and failure handling of the policy hook |
| `.venv/bin/python -m controller.summarize RUN_DIR` | Task 2.6: adaptive-run checks (ticks logged, switches applied, replay, run check) and switch statistics |
| `python3 -m controller.replay POLICY_DIR` | Re-run the policy over a run's `controller.jsonl` and compare decisions |

## Seeing priorities without `sprio`

With `DebugFlags=Priority`, `slurmctld.log` records every job's factors at every priority
calculation (`Weighted <Factor> priority is <factor> * <weight> = <value>`), then
`Job <id> priority: ... = <total>`. Timestamps are simulated time. This replaces `sprio` for
checking weight configurations (task 1.7). It is verbose (about 100k lines for 76 jobs over
6 h), so turn it on only for check runs.
