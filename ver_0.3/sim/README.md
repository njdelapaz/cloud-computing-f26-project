# Simulator (UB Slurm Simulator)

Everything here runs from `ver_0.3/`.

## Pinned sources and image

| Item | Value |
|---|---|
| `third_party/slurm_simulator` | branch `slurm-23-02-sim`, commit `f99a71eb2073266bba3869aa2a0ccd51c58c9bb7` |
| `third_party/slurm_sim_tools` | branch `v3.0-branch`, commit `21ba58d5b88b2a208a4b2db8b2b31c86d41a3847` |
| Slurm version (from `slurmctld.log`) | `23.11.0-0rc1` (the branch name says 23-02; the binary reports 23.11.0rc1) |
| Image | `slurmsim:v03`, id `sha256:daeb30c6e3f2261f7e8b654fa6fce2a3a45f73225aba4c33c16cc5aa6a444d53` |
| Image contents | Ubuntu 22.04, MariaDB, Python 3.10 with the `slurmsim` runner, Slurm built with `--enable-simulator` |

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

## Seeing priorities without `sprio`

With `DebugFlags=Priority`, `slurmctld.log` records every job's factors at every priority
calculation (`Weighted <Factor> priority is <factor> * <weight> = <value>`), then
`Job <id> priority: ... = <total>`. Timestamps are simulated time. This replaces `sprio` for
checking weight configurations (task 1.7). It is verbose (about 100k lines for 76 jobs over
6 h), so turn it on only for check runs.
