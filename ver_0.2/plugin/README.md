# Plugin forks

Both forks start from the UB simulator's Slurm source (`third_party/slurm_simulator` @ `f99a71e`). The Docker build copies them into that tree with `docker/register_plugins.sh`, so they compile with exactly the same flags as the stock plugins.

| Folder | Plugin | Forked from | Use in `slurm.conf` |
|---|---|---|---|
| `priority_adaptive/` | `priority/adaptive` | `priority/multifactor` | `PriorityType=priority/adaptive` |
| `select_cons_tres_adaptive/` | `select/cons_tres_adaptive` | `select/cons_tres` | `SelectType=select/cons_tres_adaptive` |
| `adaptive_policy/` | the shared policy state (A2), compiled into slurmctld itself | | |
| `tests/` | unit tests for the C code that runs outside slurmctld | | |

## Status
- **`priority/adaptive` (A1):** takes its ordering weights from one policy in `policies.conf`.
  - At start-up (and on `scontrol reconfigure`) it reads `adaptive.conf` from the `slurm.conf` directory, found with `get_extra_conf_path()` as the simulator finds `users.sim`, and then the `policies.conf` that `PoliciesFile` names. Relative paths in `adaptive.conf` are resolved against its own directory.
  - The active policy is `FixedPolicy` if set, otherwise `DefaultPolicy`. Its `Fairshare`, `Age` and `JobSize` replace `slurm.conf`'s `PriorityWeight*`; the Assoc, Partition, QOS and TRES weights are 0. Decay, `PriorityMaxAge`, `PriorityFlags` and the rest still come from `slurm.conf`.
  - It logs `adaptive: loaded policy=<name> mode=<FIXED|DEFAULT> Fairshare=… Age=… JobSize=…` to `slurmctld.log`.
  - A malformed file stops slurmctld with `fatal: adaptive: bad policy configuration: <file> line <n>: <problem>`. On reconfigure the error is logged and the previous weights stay.
  - With **no** `adaptive.conf` it logs that and uses `slurm.conf`'s weights, so it behaves exactly like multifactor (the P1 fork checks rely on this). `scripts/sim_run.sh` fails any run given `ADAPTIVE`/`ADAPTIVE_SET` whose log lacks the `loaded policy` line.
  - The parser (`adaptive_conf.c`) is plain C with no Slurm headers, follows the rules of `scripts/validate.py` (SPEC §13.1), and is unit-tested by `tests/test_adaptive_conf.py`. It parses all of `adaptive.conf`, `policies.conf` and `schedule.conf`.
- **`priority/adaptive` (A2):** switches policies at runtime.
  - **Controller tick:** once per `PriorityCalcPeriod`, inside the decay loop (`_controller_tick()`, called just before the Fair Tree recalculation), so it runs on the simulator's clock. It calls `adaptive_decide()` in `adaptive_ctl.c`, a pure function with no Slurm locks or globals (`decide(snapshot, config, now, controller_state)`, SPEC §4), and applies its decision.
  - **Modes** (rules in `adaptive_ctl.h`): `OVERRIDE` while a `schedule.conf` window covers the tick, in every mode; `AUTO` with the `TMin` cooldown, sampling only on eligible ticks; `FIXED` (`FixedPolicy`); `FORCED` (`ForceAlternate` + `ForcePeriod`). The first tick after a window goes straight back to the run's own policy and starts a fresh cooldown. Intervals are counted in whole ticks.
  - **AUTO in A2** has no sampler and keeps `DefaultPolicy`; A3 passes its snapshot function and classifier to `adaptive_decide()`.
  - **Hot swap:** under the job write lock, the new policy is published to the shared state and its ordering weights replace the old ones; if they differ, every pending job's priority is recomputed in one pass. `swap_us` is that work's `CLOCK_MONOTONIC` time. Nothing is charged to the virtual clock (no `sim_charge()`).
  - **Decision log:** one row per tick at `LogFile`, exactly SPEC §13.3, line-buffered. Switches are also logged to `slurmctld.log` as `adaptive: switch A -> B at sim_time=… (MODE): …, N pending jobs, swap_us=…`.
  - Start-up logs `adaptive: loaded policy=<name> mode=<DEFAULT|FIXED|FORCED>` and `adaptive: controller mode=… TMin=… tick=… windows=…`. On `scontrol reconfigure` the controller keeps its state and the policy in force, if the new `policies.conf` still has it.
  - No state signals yet (A3). The P1 clock probe in the decay loop is still there.
- **Shared policy state (`adaptive_policy/`, A2):** `adaptive_policy_publish()`, `adaptive_policy_get()`, `adaptive_policy_seq()` and `adaptive_policy_clear()`, guarded by a mutex. `docker/register_plugins.sh` copies the folder to `src/slurmctld/adaptive_policy/` and adds it to slurmctld's sources, so the process has exactly one copy. slurmctld is linked with `-export-dynamic`, so both plugins call these functions the way they call `lock_slurmctld()`. Until something is published (no `adaptive.conf`, or stock `priority/multifactor`), `adaptive_policy_get()` returns -1 and a reader should use stock placement. `tests/test_adaptive_policy.py` checks the set-up: two `dlopen`'d plugins share one state, and concurrent readers never see a torn copy.
- **`select/cons_tres_adaptive`:** still a no-op fork (placement comes in A4). A4 reads the placement weights with `adaptive_policy_get()` (add `-I$(top_srcdir)/src/slurmctld/adaptive_policy` to its `Makefile.am`).
- **Tests** (`tests/`, run inside the image by `scripts/check_a2.sh`): `test_decide.py` (the controller's table, through `adaptive_ctl_cli.c`), `test_adaptive_conf.py` (the parser) and `test_adaptive_policy.py` (the shared state).

## Constraints found in P1
- **The select fork's name has to contain `cons_tres`.** slurmctld turns on several cons_tres-only code paths by checking whether `SelectType` contains that string (in `gres_ctld.c`, `job_scheduler.c`, `node_mgr.c` and `read_config.c`).
- **The select fork needs its own `plugin_id` (150).** At start-up slurmctld loads every select plugin in the plugin directory and stops with an error if two share an id.
- **The priority fork must keep the names `decay_handler_thread` and `_decay_thread`.** The simulator replaces that thread with its own scheduled events by matching those names (`contribs/sim/slurmctld_controller.c`).
- **Simulated time:** in simulator builds, `time()`, `gettimeofday()` and the sleep calls return simulated time through `-Wl,-wrap` link flags. `clock_gettime()` isn't wrapped, so `CLOCK_MONOTONIC` gives real elapsed time, which is the clock to use for overhead measurements.
