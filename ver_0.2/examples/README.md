# Interface examples

Small hand-made files, one per data or log format in SPEC §13. `scripts/validate.py examples` checks every one of them, and the unit tests in `tests/` start from them.

| File | Format |
|---|---|
| `jobs.csv` | neutral trace: one row per job, written by the converter (B1) |
| `meta.json` | window metadata for one sampled trace |
| `episodes.csv` | trigger episodes and their onset times |
| `decisions.csv` | controller decision log, one row per tick. The example uses `TMin=5min` and a one-off override window from 06:00 to 06:03 |
| `placements.csv` | placement log, one row per job start |

The decision-log example walks through each controller rule in order:
1. sample, then switch on a fairness trigger
2. cooldown with no sampling
3. hysteresis: stay on F above the Exit threshold, then return to BASE below it
4. an override window that starts during a cooldown (override wins)
5. the window's end: back to `DefaultPolicy`, with a fresh cooldown
