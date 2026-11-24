# Plugin forks

Both forks start from the UB simulator's Slurm source (`third_party/slurm_simulator` @ `f99a71e`). The Docker build copies them into that tree with `docker/register_plugins.sh`, so they compile with exactly the same flags as the stock plugins.

| Folder | Plugin | Forked from | Use in `slurm.conf` |
|---|---|---|---|
| `priority_adaptive/` | `priority/adaptive` | `priority/multifactor` | `PriorityType=priority/adaptive` |
| `select_cons_tres_adaptive/` | `select/cons_tres_adaptive` | `select/cons_tres` | `SelectType=select/cons_tres_adaptive` |

As of P1 both are no-op forks: they behave exactly like the stock plugins. The priority fork also logs a clock probe on every decay pass.

## Constraints found in P1
- **The select fork's name has to contain `cons_tres`.** slurmctld turns on several cons_tres-only code paths by checking whether `SelectType` contains that string (in `gres_ctld.c`, `job_scheduler.c`, `node_mgr.c` and `read_config.c`).
- **The select fork needs its own `plugin_id` (150).** At start-up slurmctld loads every select plugin in the plugin directory and stops with an error if two share an id.
- **The priority fork must keep the names `decay_handler_thread` and `_decay_thread`.** The simulator replaces that thread with its own scheduled events by matching those names (`contribs/sim/slurmctld_controller.c`).
- **Simulated time:** in simulator builds, `time()`, `gettimeofday()` and the sleep calls return simulated time through `-Wl,-wrap` link flags. `clock_gettime()` isn't wrapped, so `CLOCK_MONOTONIC` gives real elapsed time, which is the clock to use for overhead measurements.
