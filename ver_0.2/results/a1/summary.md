# A1: ordering plugin, static

`priority/adaptive` now takes its ordering weights from one policy in `policies.conf`. With `BASE` loaded it matches stock `priority/multifactor` exactly, on the smoke trace and on the one-day probe trace.

Gate status: checks 1, 3, 4 and 5 pass (`results/a1/check_a1.txt`, from a `SKIP_PROBE=1` run). Check 2, the probe-day pair, was still running when the session stopped and isn't confirmed yet.

## What was built
- **Config parser** (`plugin/priority_adaptive/adaptive_conf.{c,h}`):
  - Plain C with libc only and no Slurm headers, so it builds inside the plugin and also standalone.
  - Reads `adaptive.conf` and `policies.conf` with the SPEC §13.1 grammar and the same rules as `scripts/validate.py`, including the full-set-of-11 check for policies, the weight sums, `TMin`/`ForcePeriod` as whole multiples of `PriorityCalcPeriod`, and the `FixedPolicy`/`ForceAlternate` rules.
  - It already parses every `adaptive.conf` key (signals, `TMin`, `ForceAlternate`, log paths), so A2 and A3 can use it without another parser.
  - Relative paths in `adaptive.conf` resolve against the directory `adaptive.conf` is in.
- **Plugin** (`plugin/priority_adaptive/priority_adaptive.c`):
  - `_load_policy()` finds `adaptive.conf` with `get_extra_conf_path()`, the way the simulator finds `users.sim`. It is called from `_internal_setup()`, which runs at start-up and on `scontrol reconfigure`.
  - The active policy is `FixedPolicy` if set, otherwise `DefaultPolicy`. Its `Fairshare`, `Age` and `JobSize` replace `slurm.conf`'s `PriorityWeight*`. The Assoc, Partition, QOS and TRES weights are set to 0. Decay, `PriorityMaxAge`, `PriorityFlags` and the rest still come from `slurm.conf`.
  - Start-up log line: `adaptive: loaded policy=BASE mode=DEFAULT Fairshare=70000 Age=20000 JobSize=10000 from <path> (11 policies); ...`
  - A malformed file is fatal at start-up, for example `fatal: adaptive: bad policy configuration: <run>/etc/policies.conf line 5: F: Fairshare+Age+JobSize = 100001, must be 100000`.
  - `decay_handler_thread`, `_decay_thread` and the P1 clock probe are unchanged.
- **Parser tests** (`plugin/tests/`):
  - `adaptive_conf_cli.c` loads a config with the plugin's own parser and prints the policy it would pick.
  - `test_adaptive_conf.py` has 47 tests: files that load (every policy can be fixed, `FixedPolicy` wins over `DefaultPolicy`, relative and absolute paths, comments, tabs and CRLF), and 39 broken files.
  - Every broken file is also run through `validate.py`, which must reject it too. Two cases are C-only: `validate.py` doesn't open files, and it accepts `nan`.
  - `check_a1.sh` runs the tests inside the image, with the compiler the plugin is built with.
- **Run script** (`scripts/sim_run.sh`): two new optional environment variables. Existing calls behave as before.
  - `ADAPTIVE=<adaptive.conf>` copies it into the run's `etc/`, plus the `PoliciesFile` and `ScheduleFile` it names. It points those paths at `etc/`, and `LogFile`/`PlacementLog` at `log/`.
  - `ADAPTIVE_SET="Key=Value ..."` replaces or adds keys in that copy, for example `DefaultPolicy=BASE` or `FixedPolicy=U`. It implies `ADAPTIVE=config/adaptive.conf`.
  - A run given either one fails if `slurmctld.log` has `fatal: adaptive:` or lacks the `loaded policy` line, so a policy run can't silently fall back to `slurm.conf`'s weights.
  - A non-zero simulator exit is now reported with a message (before, `set -e` exited silently).
- **Gate** (`scripts/check_a1.sh`, `scripts/check_a1.py`): described below.
- `plugin/README.md` describes the A1 behaviour.

## Gate results
Each stock/adaptive pair runs back to back. `check_a1.sh` reruns a pair once if it differs; the smoke pair didn't need a rerun.

| # | Check | Result |
|---|---|---|
| 1 | Smoke trace: stock multifactor with BASE's weights in `slurm.conf` vs `priority/adaptive` with `DefaultPolicy=BASE` and JobSize-only weights in `slurm.conf` | PASS: 7 of 7 jobs identical in start, end and nodes. A stock run under the JobSize-only weights gives a different order (1003 1005 1007 1004 1006 1002 vs BASE's 1003 1007 1004 1006 1005 1002), so the match isn't an accident of the trace |
| 2 | One-day probe trace on the 16-node project cluster, same pair | NOT YET CONFIRMED: the session hit its usage limit while this pair was running. Rerun `scripts/check_a1.sh` |
| 3 | `FixedPolicy=U` (Age only) over `DefaultPolicy=BASE`, `slurm.conf` still JobSize only | PASS: the plugin loads U in FIXED mode and jobs start in submission order (1002–1007) |
| 4 | Malformed `policies.conf` (F's weights sum to 100,001) | PASS: slurmctld stops at start-up with the file, line and problem; no jobs run |
| 5 | Parser unit tests | PASS: 47 tests |

Also checked:
- **`scripts/check_p1.sh` on the new image** (`IMAGE=ver02/slurm-sim:a1`) passes 7 of 7 (`results/a1/check_p1_on_a1_image.txt`). Its output is byte-identical to the committed `results/p1/check_p1.txt`. The fork runs there have no `adaptive.conf`, so they use the fallback below.
- **The image** `ver02/slurm-sim:a1` builds from `docker/Dockerfile` in about 3.5 min, with no compiler warnings from the plugin.

## Worth knowing
- **With no `adaptive.conf`, the plugin uses `slurm.conf`'s weights (stock behaviour) and logs that.** This is what keeps `check_p1.sh`'s fork runs passing unchanged. SPEC §13.1 doesn't say what happens when the file is absent. This is a candidate for the decisions log. The guard against misuse is in `sim_run.sh`: any run given a policy must log `loaded policy`.
- **On `scontrol reconfigure` a malformed file is logged as an error and the previous weights stay**, the way slurmctld treats a bad `slurm.conf` on reconfigure. Only start-up is fatal. The simulator never reconfigures, so this path is untested.
- **When slurmctld dies at start-up, the simulator exits instead of hanging.** `slurmsim` ends about 20 s in, with a Python traceback about the missing `jobcomp.log`. No timeout handling is needed. `check_a1.sh` still wraps that run in `timeout 600`.
- **The parser is slightly stricter than `validate.py`.** It rejects `nan` and `inf`, takes integers as plain digits only (Python's `int()` also accepts `+5` and `1_000`), and stops at the first problem.
- **`sprio -w` and `scontrol show config` still show `slurm.conf`'s weights.** They read `slurm_conf`, not the plugin. This doesn't affect the simulator, but A2's debugging should use the plugin's log line.
- **The startup log's mode is `DEFAULT` when `FixedPolicy` is absent.** This isn't one of the decision-log modes. A2 turns it into `AUTO` with switching. `ScheduleFile` is copied into each run but not parsed yet (A2), and `LogFile` points at the run's `log/` but isn't written yet.
- **Gate trace:** WORKFLOW A1 asks for a W0 trace. B1's W0 traces don't exist yet, so check 2 uses the P1 probe day (`workloads/traces/p1_probe`, 3,028 jobs, 2024-01-15, seed 1). Once B1 delivers W0, the same pair can be rerun on it by pointing `PROBE` in `check_a1.sh` at the new trace folder and updating the expected job count in `check_a1.py`.
- **Slurm prefixes plugin log lines,** so they appear as `priority/adaptive: _load_policy: adaptive: ...`. The fatal line has no prefix.
