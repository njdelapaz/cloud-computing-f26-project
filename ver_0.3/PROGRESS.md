# PROGRESS — ver_0.3

Status tracker for [PLAN.md](PLAN.md). It records status, decisions and results only; no task
lengths or dates beyond the change log.

**Current phase:** 1 — Workloads, classes, weight configurations (Steps 1–3)

## Phase tracker

| Task | Status | Notes |
|---|---|---|
| 1.1 Folder setup and tooling | done | Layout, `.venv`, pinned `requirements.txt`, `pytest.ini`, layout tests (3 pass) |
| 1.2 Simulator bring-up | in progress | |
| 1.3 Trace profiling | not started | |
| 1.4 Job classifier | not started | |
| 1.5 Workload builder | not started | |
| 1.6 Weight configurations | not started | |
| 1.7 Configuration check in the simulator | not started | |
| 2.1 Specify Steps 4–5 | not started | waits for Phase 1 |
| 2.2 Determine trigger states | not started | |
| 2.3 Build the change mechanism | not started | |
| 3.1 Specify Step 6 | not started | waits for Phase 2 |
| 3.2 Run the simulations | not started | |
| 3.3 Scale up | not started | |
| 3.4 Analyze and report | not started | |

## Pinned third-party sources

| Checkout (in `third_party/`) | Branch | Commit |
|---|---|---|
| `slurm_simulator` (UB Slurm Simulator, Slurm 23.02 based) | `slurm-23-02-sim` | `f99a71eb2073266bba3869aa2a0ccd51c58c9bb7` |
| `slurm_sim_tools` (runner and tools, v3.0) | `v3.0-branch` | `21ba58d5b88b2a208a4b2db8b2b31c86d41a3847` |

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Folder names | `third_party/`, `workloads/` (`cache/`, `traces/`), `results/<group>/<run>/` | Match the committed `ver_0.3/.gitignore`. |
| Simulator image | Own lean image from `sim/Dockerfile` (`slurmsim:v03`) built from the pinned checkouts | The upstream image adds Jupyter, RStudio, Eclipse and Chrome, which the simulation does not need. |
| Python | 3.12 venv in `.venv/`, pinned `requirements.txt` | SPEC conventions. |

## Change log

| Date | Commit | Change |
|---|---|---|
