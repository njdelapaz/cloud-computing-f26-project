# A1: ordering plugin, static

`priority/adaptive` now takes its ordering weights from one policy in `policies.conf`. With `BASE` loaded it matches stock `priority/multifactor`. On the smoke trace the match is exact. On a contended trace it matches to within the simulator's own 1 s clock jitter.

The one-day probe trace couldn't be used for the identity check, because the simulator doesn't repeat itself on realistic load. Even two identical stock runs differ on most jobs. That finding matters beyond A1, and is described below.

**Gate:** `scripts/check_a1.sh` passes all 5 gating checks (`results/a1/check_a1.txt`). Check 2 now uses the contended trace instead of the probe day. The A1 session proposed that change, and the user answered "continue". WORKFLOW's A1 gate text still names a W0 trace and needs updating in the main session to match.

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
- **Contended trace** (`config/smoke/contended.events`, written by `scripts/make_contended_events.py` with seed 7):
  - 41 jobs on the 3-node, 12-core smoke cluster. A 12-core job from user1 fills the cluster for 30 min while 40 jobs from the four smoke users (two accounts) queue behind it.
  - 40 of the 41 jobs wait more than 60 s, so ordering decides nearly every start.
  - `check_a1.sh` checks that the committed file matches the script.
- **Gate** (`scripts/check_a1.sh`, `scripts/check_a1.py`): about 10 min; `PROBE=1` adds the non-gating probe-day runs (about 33 min more).
- `plugin/README.md` describes the A1 behaviour.

## Gate results
In every adaptive run, `slurm.conf` holds JobSize-only weights (Age=0, Fairshare=0, JobSize=1,000,000), so the policy file is the only source of BASE's weights.

| # | Check | Result |
|---|---|---|
| 1 | Smoke trace (7 jobs): stock multifactor with BASE's weights in `slurm.conf` vs `priority/adaptive` with `DefaultPolicy=BASE` | PASS, exact: all 7 jobs identical in start, end and nodes. The pair needed no rerun. A stock run under the JobSize-only weights gives a different order (1003 1005 1007 1004 1006 1002 vs BASE's 1003 1007 1004 1006 1005 1002) |
| 2 | Contended trace (41 jobs): 3 stock repeats, adaptive BASE, and a JobSize-only stock control | PASS within 1 s. 40 of 41 jobs wait more than 60 s. Stock repeats 2 and 3, and adaptive, each match stock run 1 with 0 node differences, 0 starts or ends more than 1 s off, and 0 start-order flips. Adaptive's 5 exact-match differences are all 1 s shifts, the same kind the stock repeats show. The JobSize-only control has 23 node differences, 39 jobs moved by more than 1 s and 333 start-order flips, so the check is sharp |
| 3 | `FixedPolicy=U` (Age only) over `DefaultPolicy=BASE` | PASS: the plugin loads U in FIXED mode and jobs start in submission order (1002–1007) |
| 4 | Malformed `policies.conf` (F's weights sum to 100,001) | PASS: slurmctld stops at start-up with the file, line and problem; no jobs run |
| 5 | Parser unit tests | PASS: 47 tests |
| — | One-day probe trace (not gating) | INFO: see the next section |

Also checked:
- **`scripts/check_p1.sh` on the new image** (`IMAGE=ver02/slurm-sim:a1`) passes 7 of 7 (`results/a1/check_p1_on_a1_image.txt`). Its output is byte-identical to the committed `results/p1/check_p1.txt`. The fork runs there have no `adaptive.conf`, so they use the fallback described below.
- **The image** `ver02/slurm-sim:a1` builds from `docker/Dockerfile` in about 3.5 min, with no compiler warnings from the plugin.

## The simulator doesn't repeat itself on realistic load
This is the most important finding of A1, and it affects P4–P8.

**Probe-day runs** (`workloads/traces/p1_probe`: 3,028 jobs on 2024-01-15, seed 1, 16-node project cluster). Each run took about 11 min. The host was nearly idle (load average about 1), apart from a few tiny smoke runs alongside `probe_stock_base_2`.

| Run folder | Configuration | Mean wait | p95 wait | Jobs that differ from `probe_stock_base` |
|---|---|---|---|---|
| `probe_stock_base` | BASE weights in `slurm.conf` | 1,099 s | 4,589 s | — |
| `probe_stock_base_2` | the same | 5,727 s | 49,941 s | 2,110 |
| `probe_adaptive_base` | `DefaultPolicy=BASE` | 2,759 s | 11,436 s | 1,932 |
| `probe_adaptive_base_2` | the same | 1,895 s | 8,191 s | 1,726 |

- **Identical configurations differ as much as stock vs adaptive does.** Mean wait spans 1.1k–5.7k s over four runs whose ordering weights are all BASE's.
- **Every pair first diverges at a 1–2 s shift of a job that starts the moment it's submitted.** Stock 1 vs stock 2 diverge at `jobid_1`. The other pairs diverge at `jobid_247` (03:53) or `jobid_297` (04:14). No queue exists at those points: none of the 246 jobs that start before 03:53 waited more than 60 s. Queues first form around 10:11, after the runs have already drifted apart. So on this trace even the stretch that matched never tested the ordering.
- **Cause** (`third_party/slurm_simulator/contribs/sim/sim_time.c`): the simulated clock is `real_time × ClockScaling + shift`. The event loop jumps over idle gaps, but while slurmctld is working the clock runs at real speed. A scheduling pass that crosses a second boundary shifts a start by 1 s. On a busy 16-node cluster that shift changes later backfill decisions, and the difference grows from there.
- **A slower clock doesn't fix it.** At `ClockScaling=0.001` with `TimeAfterAllEventsDone=0`, runs are fast (the smoke trace takes 18 s of real time), but two identical stock smoke runs still differ by 1 s on 5 of 7 jobs, so threads racing against the clock also play a part. With the default 30 s `TimeAfterAllEventsDone`, the end-of-run tail isn't skipped and crawls 1000× slower.
- **On the contended smoke trace the jitter stays at 1 s and doesn't grow.** Identical stock runs differ by 1 s on 4–6 of 41 jobs, and never in nodes or start order. P1's "three identical repeats" came from the 7-job smoke trace and doesn't carry over to realistic load.
- **What this means for P4–P8:** repeating a run with the same seed and configuration can change mean wait by a factor of five. Policy comparisons need repeats per seed, or many more seeds, before differences mean anything. The alternative is making the simulator deterministic, which means changing the pinned simulator. This needs a decision in the main session before P4.

## Worth knowing
- **With no `adaptive.conf`, the plugin uses `slurm.conf`'s weights (stock behaviour) and logs that.** This is what keeps `check_p1.sh`'s fork runs passing unchanged. SPEC §13.1 doesn't say what happens when the file is absent, so this is a candidate for the decisions log. The guard against misuse is in `sim_run.sh`: any run given a policy must log `loaded policy`.
- **On `scontrol reconfigure` a malformed file is logged as an error and the previous weights stay,** the way slurmctld treats a bad `slurm.conf` on reconfigure. Only start-up is fatal. The simulator never reconfigures, so this path is untested.
- **When slurmctld dies at start-up, the simulator exits rather than hanging.** `slurmsim` ends about 20 s in, with a Python traceback about the missing `jobcomp.log`.
- **The parser is slightly stricter than `validate.py`.** It rejects `nan` and `inf`, takes integers as plain digits only (Python's `int()` also accepts `+5` and `1_000`), and stops at the first problem.
- **`sprio -w` and `scontrol show config` still show `slurm.conf`'s weights,** because they read `slurm_conf`, not the plugin. Use the plugin's log line instead.
- **The startup log's mode is `DEFAULT` when `FixedPolicy` is absent.** This isn't one of the decision-log modes; A2 turns it into `AUTO` with switching. `ScheduleFile` is copied into each run but not parsed yet (A2), and `LogFile` points at the run's `log/` but isn't written yet.
- **Slurm prefixes plugin log lines,** so they appear as `priority/adaptive: _load_policy: adaptive: ...`. The fatal line has no prefix.
- **The probe-day runs in `check_a1.txt` are older than the final gate run.** They come from the earlier full gate run and one extra stock repeat, all with the same image and plugin code. `check_a1.sh` lists them when their folders are present; `PROBE=1` regenerates them.
