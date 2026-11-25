# B2 hand-computed trace

The B2 gate (WORKFLOW B2) runs `analysis/metrics.py` on this trace and requires every output column to match `hand.json`, which holds the values worked out below. `tests/test_metrics.py` checks the same values.

- `trace/` holds the workload: `meta.json` and `jobs.csv`, in SPEC §13.2's formats.
- `run/` is laid out like a run folder from `scripts/sim_run.sh`:
  - `etc/slurm.conf` defines the nodes;
  - `log/jobcomp.log` holds the completion records;
  - `log/decisions.csv` and `log/placements.csv` are the SPEC §13.3 logs;
  - `log/queue_dumps.csv` uses the format proposed in `analysis/metrics.py`;
  - `log/cost.csv` is the cost log.
- Every file is hand-written, not simulator output. All of them pass `scripts/validate.py`'s checks for their format.

All times below are seconds after T0 = 1704067200 (2024-01-01T00:00:00Z).

## Setup
- **Cluster:** the smoke cluster, 3 nodes (n1–n3), each with 4 cores and 16,000 MB, so 12 cores and 48,000 MB in total. Node load is L = max(cores ÷ 4, MB ÷ 16,000).
- **Window:** `warmup_end` = 100 and `window_end` = 1100, so the window is 1,000 s.
- **Decision log:** forced alternation between U and F, one tick every 60 s from 60 to 1200.
  - Switches happen at 60 (BASE→U), 240 (U→F), 480 (F→U), 720 (U→F), 960 (F→U) and 1200 (U→F).
  - Their `swap_us` values are 500, 800, 1000, 600, 900 and 5000.

| Job | JobId | User | Submit | Start | End | Node | Cores | MB | Runtime | Measured |
|---|---|---|---|---|---|---|---|---|---|---|
| J1 | 1001 | user0001 | 0 | 0 | 600 | n1 | 4 | 4000 | 600 | no, warm-up |
| J2 | 1002 | user0002 | 50 | 50 | 300 | n3 | 3 | 3000 | 250 | no, warm-up |
| J3 | 1003 | user0001 | 100 | 100 | 400 | n2 | 2 | 12000 | 300 | yes |
| J4 | 1004 | user0003 | 200 | 300 | 800 | n3 | 2 | 8000 | 500 | yes |
| J5 | 1005 | user0002 | 230 | 400 | 1200 | n2 | 3 | 3000 | 800 | yes |
| J6 | 1006 | user0001 | 440 | 600 | 1050 | n1 | 4 | 4000 | 450 | yes |
| J7 | 1007 | user0003 | 650 | 650 | 654 | n3 | 1 | 1000 | 4 | yes |

Why the jobs wait:
- **J4** needs 2 cores and 8,000 MB. At 200, n1 is full, n2 has 2 cores free but only 4,000 MB, and n3 has 1 core free. It starts on n3 when J2 ends.
- **J5** needs 3 cores, but no node has more than 2 free until J3 ends at 400.
- **J6** needs a whole node, and gets n1 when J1 ends at 600.

## Job metrics (measured jobs J3–J7)
**Waits:** J3 0, J4 100, J5 170, J6 160, J7 0. Sorted, that is 0, 0, 100, 160, 170.
- mean = 430 ÷ 5 = **86**
- median = **100**
- p95: the position is 0.95 × 4 = 3.8, so 160 + 0.8 × (170 − 160) = **168**
- max = **170**

**Bounded slowdown,** max(1, (wait + runtime) ÷ max(runtime, 10)):

| Job | Calculation | Result |
|---|---|---|
| J3 | 300 ÷ 300 | 1 |
| J4 | 600 ÷ 500 | 6/5 |
| J5 | 970 ÷ 800 | 97/80 |
| J6 | 610 ÷ 450 | 61/45 |
| J7 | max(1, 4 ÷ 10) | 1 |

The mean is (720 + 864 + 873 + 976 + 720) ÷ 720 ÷ 5 = 4153/3600 = **1.153611**.

**Jain index** over each user's mean bounded slowdown:
- user0001 (J3, J6): (1 + 61/45) ÷ 2 = 53/45
- user0002 (J5): 97/80
- user0003 (J4, J7): (6/5 + 1) ÷ 2 = 11/10
- Σx = (848 + 873 + 792) ÷ 720 = 2513/720
- Σx² = (719,104 + 762,129 + 627,264) ÷ 518,400 = 2,108,497/518,400
- Jain = (2513/720)² ÷ (3 × 2,108,497/518,400) = 6,315,169 ÷ 6,325,491 = **0.998368**

**Wait spread:** the users' mean waits are 80 (user0001), 170 (user0002) and 50 (user0003). The worst is **170**, the best is **50**, and the spread is 170 ÷ 50 = **3.4**.

**Makespan:** the last end is 1200 (J5) and the first submit is 100 (J3), so makespan = **1,100 s**.

**Throughput:** 5 jobs ÷ (1,100 ÷ 86,400 days) = 4320/11 = **392.727 jobs per day**.

## Node metrics, integrated over [100, 1100)
**Node loads:**
- n1 holds J1, then J6, both at load 1. It is idle (load 0) from 1050.
- n2 holds J3 (max(0.5, 0.75) = 0.75), then J5 (max(0.75, 0.1875) = 0.75).
- n3 holds:
  - J2: load 0.75, until 300
  - J4: load 0.5
  - J4 plus J7, from 650 to 654: 3 cores and 9,000 MB, so load 0.75
  - nothing from 800

| Interval | Length (s) | (L₁, L₂, L₃) | Population SD | Max |
|---|---|---|---|---|
| [100, 300) | 200 | (1, 0.75, 0.75) | √(1/72) = 0.117851 | 1 |
| [300, 650) | 350 | (1, 0.75, 0.5) | √(1/24) = 0.204124 | 1 |
| [650, 654) | 4 | (1, 0.75, 0.75) | √(1/72) | 1 |
| [654, 800) | 146 | (1, 0.75, 0.5) | √(1/24) | 1 |
| [800, 1050) | 250 | (1, 0.75, 0) | √(13/72) = 0.424918 | 1 |
| [1050, 1100) | 50 | (0, 0.75, 0) | √(1/8) = 0.353553 | 0.75 |

**Balance:**
- load SD = (204·√(1/72) + 496·√(1/24) + 250·√(13/72) + 50·√(1/8)) ÷ 1000 = **0.249194**
- max load = (950 × 1 + 50 × 0.75) ÷ 1000 = **0.9875**

**Core utilization:**
- n1: 4 cores × 950 s = 3,800
- n2: 2 × 300 + 3 × 700 = 2,700
- n3: 3 × 200 (J2) + 2 × 500 (J4) + 1 × 4 (J7) = 1,604
- total: 8,104 core-seconds, and 8,104 ÷ (12 × 1000) = **0.675333**

**Memory utilization:**
- n1: 4,000 × 950 = 3,800,000
- n2: 12,000 × 300 + 3,000 × 700 = 5,700,000
- n3: 3,000 × 200 + 8,000 × 500 + 1,000 × 4 = 4,604,000
- total: 14,104,000 MB-seconds, and 14,104,000 ÷ (48,000 × 1000) = **0.293833**

**Stranded cores.** A node's free cores count while jobs are pending if its free MB per free core is below the median MB per core of the pending jobs:

| Interval | Pending (MB per core) | Median | n2 free | n3 free | Stranded |
|---|---|---|---|---|---|
| [100, 200) | none | — | 2 cores, 4,000 MB | 1 core, 13,000 MB | 0 (nothing pending) |
| [200, 230) | J4 (4000) | 4000 | 2,000 per core < 4,000: 2 | 13,000 per core: 0 | 2 × 30 = 60 |
| [230, 300) | J4 (4000), J5 (1000) | 2500 | 2,000 < 2,500: 2 | 0 | 2 × 70 = 140 |
| [300, 400) | J5 (1000) | 1000 | 2,000 ≥ 1,000: 0 | 2 cores, 8,000 MB: 0 | 0 |
| [440, 600) | J6 (1000) | 1000 | 1 core, 13,000 MB: 0 | 4,000 per core: 0 | 0 |

n1 has no free cores while anything is pending. Nothing is pending in [400, 440) or from 600 on. The total is 200 core-seconds, so stranded = 200 ÷ 3600 = 1/18 = **0.055556 core-hours**.

## Overhead (rows in [100, 1100))
**Switches:** 240, 480, 720 and 960 are in the window; 60 and 1200 are outside. So **4** switches, with `swap_us` 600, 800, 900 and 1000:
- mean = **825**
- p95: the position is 0.95 × 3 = 2.85, so 900 + 0.85 × 100 = **985**
- max = **1000**

**Placements:** J3–J7 started in the window, so **5** placements. J1 and J2 started before it.
- J3, J6 and J7 were placed under U, which scores. J4 and J5 were placed under F, which uses stock placement and logs `score_us` 0.
- So score_n = **3**, with `score_us` 15, 25 and 11:
  - mean = **17**
  - p95: the position is 1.9, so 15 + 0.9 × 10 = **24**
  - max = **25**

**Churn:**
- At 240 the queue is J4, J5 before and J5, J4 after. The one pair flipped, so churn is 1/1 = 1.
- At 480 only J6 is pending, so there are no pairs and that switch gives no churn value.
- So churn_n = **1**, mean = **1**, max = **1**.

**Cost log, main scheduler (`sched`):**
- The rows at 50 (999 µs) and 1150 (7,777 µs) are outside the window, and the cut-off last line is skipped.
- The **9** rows in the window are 100, 200, 300, 150, 250, 120, 180, 90 and 60 µs:
  - mean = 1450 ÷ 9 = **161.111**
  - p95: sorted, the position is 0.95 × 8 = 7.6, so 250 + 0.6 × 50 = **280**
  - max = **300**
- The last pass before each switch and the first pass at or after it:

  | Switch | Pass before | Pass after |
  |---|---|---|
  | 240 | 230 (200) | 250 (300) |
  | 480 | 470 (150) | 500 (250) |
  | 720 | 700 (120) | 730 (180) |
  | 960 | 950 (90) | 1000 (60) |

  So before = 560 ÷ 4 = **140** and after = 790 ÷ 4 = **197.5**.

**Cost log, backfill (`bf`):**
- The rows at 90 and 1120 are outside the window.
- The **8** rows in the window are 400, 600, 500, 700, 450, 550, 350 and 650 µs:
  - mean = **525**
  - p95: the position is 0.95 × 7 = 6.65, so 650 + 0.65 × 50 = **682.5**
  - max = **700**
- The adjacent cycles are (200: 400, 245: 600), (475: 500, 485: 700), (715: 450, 725: 550) and (955: 350, 965: 650). So before = 1700 ÷ 4 = **425** and after = 2500 ÷ 4 = **625**.

## `examples/` logs (no window)
**`examples/decisions.csv`:** **4** switches, with `swap_us` 812, 795, 801 and 790 (sorted: 790, 795, 801, 812).
- mean = 3198 ÷ 4 = **799.5**
- p95: the position is 2.85, so 801 + 0.85 × 11 = **810.35**
- max = **812**

**`examples/placements.csv`:** **7** placements, 3 of them scored (jobs 105–107), with `score_us` 14, 12 and 11.
- mean = 37 ÷ 3 = **12.333**
- p95: sorted, the position is 1.9, so 12 + 0.9 × 2 = **13.8**
- max = **14**
