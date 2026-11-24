# Plugin forks

Both forks start from the UB simulator's Slurm source (`third_party/slurm_simulator` @ `f99a71e`). The Docker build copies them into that tree with `docker/register_plugins.sh`, so they compile with exactly the same flags as the stock plugins.

| Folder | Plugin | Forked from | Use in `slurm.conf` |
|---|---|---|---|
| `priority_adaptive/` | `priority/adaptive` | `priority/multifactor` | `PriorityType=priority/adaptive` |
| `select_cons_tres_adaptive/` | `select/cons_tres_adaptive` | `select/cons_tres` | `SelectType=select/cons_tres_adaptive` |
| `tests/` | unit tests for the C code that runs outside slurmctld | | |

## Status
- **`priority/adaptive` (A1):** takes its ordering weights from one policy in `policies.conf`.
  - At start-up (and on `scontrol reconfigure`) it reads `adaptive.conf` from the `slurm.conf` directory, found with `get_extra_conf_path()` as the simulator finds `users.sim`, and then the `policies.conf` that `PoliciesFile` names. Relative paths in `adaptive.conf` are resolved against its own directory.
  - The active policy is `FixedPolicy` if set, otherwise `DefaultPolicy`. Its `Fairshare`, `Age` and `JobSize` replace `slurm.conf`'s `PriorityWeight*`; the Assoc, Partition, QOS and TRES weights are 0. Decay, `PriorityMaxAge`, `PriorityFlags` and the rest still come from `slurm.conf`.
  - It logs `adaptive: loaded policy=<name> mode=<FIXED|DEFAULT> Fairshare=… Age=… JobSize=…` to `slurmctld.log`.
  - A malformed file stops slurmctld with `fatal: adaptive: bad policy configuration: <file> line <n>: <problem>`. On reconfigure the error is logged and the previous weights stay.
  - With **no** `adaptive.conf` it logs that and uses `slurm.conf`'s weights, so it behaves exactly like multifactor (the P1 fork checks rely on this). `scripts/sim_run.sh` fails any run given `ADAPTIVE`/`ADAPTIVE_SET` whose log lacks the `loaded policy` line.
  - The parser (`adaptive_conf.c`) is plain C with no Slurm headers, follows the rules of `scripts/validate.py` (SPEC §13.1), and is unit-tested by `tests/test_adaptive_conf.py`. It parses all of `adaptive.conf`, including the keys A2 and A3 will use.
  - No switching yet (A2), no state signals (A3). The P1 clock probe in the decay loop is still there.
- **`select/cons_tres_adaptive`:** still a no-op fork (placement comes in A4).

## Constraints found in P1
- **The select fork's name has to contain `cons_tres`.** slurmctld turns on several cons_tres-only code paths by checking whether `SelectType` contains that string (in `gres_ctld.c`, `job_scheduler.c`, `node_mgr.c` and `read_config.c`).
- **The select fork needs its own `plugin_id` (150).** At start-up slurmctld loads every select plugin in the plugin directory and stops with an error if two share an id.
- **The priority fork must keep the names `decay_handler_thread` and `_decay_thread`.** The simulator replaces that thread with its own scheduled events by matching those names (`contribs/sim/slurmctld_controller.c`).
- **Simulated time:** in simulator builds, `time()`, `gettimeofday()` and the sleep calls return simulated time through `-Wl,-wrap` link flags. `clock_gettime()` isn't wrapped, so `CLOCK_MONOTONIC` gives real elapsed time, which is the clock to use for overhead measurements.
