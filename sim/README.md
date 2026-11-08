# Simulation Environment

The 24-GPU test cluster runs in the UB Slurm Simulator v3.0, with a project patch that lets the scheduling policy change during a run.

## Quick start

Requires Docker. The first run builds the patched image from `nsimakov/slurm_sim:v3.0`, which is about 24 GB.

```bash
scripts/run_sim.sh sim/workloads/test20.events
```

```bash
scripts/run_sim.sh sim/workloads/test_switch.events
```

Results go to `sim/results/<run-name>/dtstart_<dtstart>_<replica>/`:

| File | Contents |
|---|---|
| `slurm_acct.out` | `sacct` output, one row per job (`\|`-separated): submit, start and end times, nodes, CPUs, state |
| `policy_switches.csv` | Starting policy, then one row per switch: simulated time, old policy, new policy, status |
| `slurmctld.log` | Full controller log. Lines starting `SIM_SWITCH_POLICY:` show exactly what each switch changed |
| `run_sim.out` | Console output of the simulator wrapper |

Options are set through environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `SIM_START_POLICY` | `baseline` | Policy active at the start, from `sim/etc/policies/` |
| `SIM_MAX_REAL_SECONDS` | `1800` | Wall-clock limit before a run is stopped |
| `SLURM_SIM_IMAGE` | `gpu24-slurm-sim:latest` | Simulator image |

The optional arguments after the events file are `[run-name] [dtstart] [replica]`. `dtstart` is the delay before the first job. It is the simulator's main source of run-to-run variation, so repeated runs should use different values.

## Layout

```
sim/
  etc/
    slurm.conf          cluster config; includes policy_active.conf
    policies/*.conf     one file per scheduling policy (see below)
    gres.conf           8 GPUs per node, declared by count
    sacctmgr.script     users, accounts and QoS loaded before each run
    users.sim           simulated system users (name:uid:group:gid)
    slurmdbd.conf, sim.conf, slurm.key, slurm.cert
  patches/
    switch_policy.patch adds the switch_policy event (see below)
  workloads/
    specs/*.json        seeded generator specs for the starter workloads
    *.csv               workloads in the common format
    *.events            the same workloads in the simulator's events format
docker/Dockerfile       patched simulator image
scripts/
  run_sim.sh            runs one simulation
  generate_workload.py  spec -> workload .csv and .events
  convert_workload.py   workload .csv <-> .events, optional scheduled switches
  metrics.py            one run's results -> metrics.json (+ summary CSV row)
  run_baseline.sh       generate, run and measure every starter workload
  workload_format.py    the common format, shared by the scripts above
results/
  baseline.csv          baseline metrics, one row per workload
  baseline/*.json       full baseline metrics, incl. per user and per node
```

## Workloads and metrics

The scripts use only the Python standard library.

Every workload is a CSV in the common format, one row per job: `job_id,user,submit_time,gpus,runtime,time_limit` (seconds; see `scripts/workload_format.py`). A job whose `runtime` is at least its `time_limit` ends as `TIMEOUT`.

To regenerate all five starter workloads from their seeds (same seed, same files) and run the baseline on each:

```bash
scripts/run_baseline.sh
```

`SETUP=<policy> scripts/run_baseline.sh` runs the same workloads under another policy and writes `results/<policy>.csv`. To recompute metrics from existing runs without simulating again:

```bash
scripts/run_baseline.sh --metrics-only
```

The pieces can also run alone:

```bash
scripts/generate_workload.py sim/workloads/specs/heavy_small.json
```

```bash
scripts/convert_workload.py sim/workloads/heavy_small.csv -o sim/workloads/heavy_small_switch.events --switch 21600:test_lln
```

```bash
scripts/metrics.py sim/results/baseline_light/dtstart_59_1 sim/workloads/light.csv
```

The spec format and every metric are documented at the top of `generate_workload.py` and `metrics.py`.

**Per-node GPU use is reconstructed.** `slurm_acct.out` lists a job's nodes but not its GPUs on each, so `metrics.py` replays job starts and ends. Single-node jobs and multi-node jobs using whole nodes (16 or 24 GPUs) are exact. Other multi-node jobs (e.g. 12 GPUs on 2 nodes) are split as evenly as free GPUs allow and counted in `approx_split_jobs`. The starter workloads only use multiples of 8 for multi-node jobs, so they have none.

`sim/log/`, `sim/var/`, `sim/results/` and `sim/etc/policy_active.conf` are created at run time and are not committed. The summaries in `results/` are committed.

## Events file format

Each line is one event. `-dt` is the number of seconds after the first job's submission.

```
-dt 0 -e submit_batch_job | -J jobid_1001 -sim-walltime 600 --uid=user1 -t 00:15:00 -N 1 --gpus=1 -A account1 -p gpu -q normal pseudo.job
-dt 100 -e switch_policy | utilization
```

Job requests:
- **GPUs:** use `-N <fewest nodes> --gpus=<G>`, i.e. `-N 1` for 1–8 GPUs, `-N 2` for 9–16 and `-N 3` for 17–24.
  - The simulator rejects `--gpus-per-task` ("never runnable").
  - Without `-N`, a small job can be split across nodes.
  - Each job gets 8 CPUs and 64 GB per GPU automatically (`DefCpuPerGPU`, `DefMemPerCPU`).
- **`-sim-walltime`:** the job's actual runtime in seconds. **`-1`** means it runs until its time limit (`-t`) and ends as `TIMEOUT`. **`0`** means it ends instantly; the upstream tutorial says otherwise, but that is wrong.
- **`--uid`** must be a user in `users.sim`, and **`-A`** that user's account in `sacctmgr.script`.
- Lines starting with `#` are comments.

`slurm_acct.out` does not record GPUs per job, so analysis takes GPU counts from the events file.

## Policies and switching

A policy is a file in `sim/etc/policies/` written in `slurm.conf` syntax. At startup, `run_sim.sh` copies the starting policy to `sim/etc/policy_active.conf`, which `slurm.conf` includes.

A `switch_policy` event applies another policy at its simulated time. It changes only these settings:

| Setting | Effect when switched | Verified in Phase 1 |
|---|---|---|
| `PriorityWeightAge`, `...Fairshare`, `...JobSize`, `...Partition`, `...QOS`, `PriorityFavorSmall` | New weights apply, and every pending job is re-prioritized immediately | A queued 1-GPU job overtook an earlier queued 24-GPU job |
| `NodeName=<nodes> ... Weight=<n>` | Same as `scontrol update NodeName=<nodes> Weight=<n>` | New jobs moved to the lowest-weight node |
| `PartitionName=<p> ... LLN=YES\|NO` | Same as `scontrol update PartitionName=<p> LLN=...` | New jobs went to the least-loaded node |

Other keys on the `NodeName` and `PartitionName` lines describe hardware. They are ignored at a switch, so keep them identical in every policy file. Any other setting, such as `SchedulerParameters`, can't change mid-run; the switch logs an error and ignores it.

The `test_*` policies exist only for the Phase 1 switching test.

### Why a patch is needed

The simulator does not answer client commands during a run. It never starts `slurmctld`'s RPC thread, so `squeue`, `sinfo` and `scontrol` fail with "Unable to contact slurm controller".

A full reconfigure from inside the simulator doesn't work either. It crashes with `fatal: SIM:Not implemented agent request`, because it tries to message `slurmd`, and it would not reload node weights anyway.

So the patch applies a policy's settings directly, the way `scontrol update` does internally.

### Changing the patch

```bash
git clone -b slurm-23-02-sim https://github.com/ubccr-slurm-simulator/slurm_simulator.git external/slurm_simulator
```

```bash
git -C external/slurm_simulator checkout f99a71eb2073266bba3869aa2a0ccd51c58c9bb7
```

```bash
git -C external/slurm_simulator apply ../../sim/patches/switch_policy.patch
```

Edit the files under `external/slurm_simulator`, then save the patch and rebuild the image:

```bash
git -C external/slurm_simulator diff > sim/patches/switch_policy.patch
```

```bash
docker build -f docker/Dockerfile -t gpu24-slurm-sim:latest .
```

Commit `f99a71e` is the simulator source that ships in `nsimakov/slurm_sim:v3.0`; the files the patch touches were checked to be identical.

## Phase 1 findings

- **Version:** the simulator's docs say Slurm 23.02, but `slurmctld` reports **23.11.0-0rc1**.
- **Speed:** about 200x faster than real time on this cluster. The 20-job test covers 1 h 49 min of simulated time and runs in 33 s.
- **Database:** MariaDB must be started as root inside the container. `slurmdbd.conf` must have mode 600, which `run_sim.sh` sets on every run because git doesn't store it.
- **`slurmsim -rt`:** this option sets a minimum run time, not a limit. The wrapper waits that long even after the simulation ends, so `run_sim.sh` uses its own timeout instead.
- **Rejected jobs:** if every job is rejected at submit, the simulation never ends on its own. The timeout and the job-count check in `run_sim.sh` catch this.
