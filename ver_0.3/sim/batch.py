"""Task 3.2: run a batch of Phase 3 runs (SPEC §8.4) and check each one.

Usage:
  .venv/bin/python -m sim.batch --workloads N-s1 [X-s1 ...|all] [--policies all|A1,C0]
                                [--role matrix|pilot|repeat] [--parallel 9] [--dry-run]
                                [--recheck]     re-run the checks of finished runs only

Policies, workloads, start times and the run folder come from config/evaluation.yaml. A run
of policy P on workload W goes to <run_root>/W/P (role matrix), W/P-pilot or W/P-repeat.

For each run: prepare (sim.prepare_run), simulate (sim/run_sim.sh with the slurmsim:v03
image), then check: sim.check_run for every run, and controller.summarize for adaptive runs
(every tick logged on both sides, every switch applied, replay exact). A run that fails is
rerun once; the failed attempt's folder is kept as <run>.attempt1. A run still going after
run_timeout_h hours of real time is stopped and counts as failed (a simulator that keeps
jobs it never ran in its active list never ends, task 3.4). Containers start at least
start_gap_s apart, and a run whose controller refuses any submission is stopped at once and
retried: with dozens of runs starting together, slurmctld can start before the accounts are
loaded and then refuses every job (task 3.5). Each run folder gets done.json
(outcome, attempt, image id, real time, peak container memory, mean CPU, disk use); a run
whose done.json says ok is skipped, so a batch can be resumed. One line per attempt is
appended to results/phase3/batches.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sim.prepare_run import ROOT, prepare_run

EVAL = ROOT / "config" / "evaluation.yaml"
LOG = ROOT / "results" / "phase3" / "batches.jsonl"
IMAGE = os.environ.get("SLURMSIM_IMAGE", "slurmsim:v03")
SUFFIX = {"matrix": "", "pilot": "-pilot", "repeat": "-repeat"}
_check_lock = threading.Lock()       # sim.check_run keeps its known issues in a module list
_log_lock = threading.Lock()
_start_lock = threading.Lock()       # spaces out container starts
_last_start = [0.0]
REFUSED = "Batch job submission failed"


def wait_start_slot(gap_s: float) -> None:
    with _start_lock:
        wait = _last_start[0] + gap_s - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_start[0] = time.time()


def refused_submissions(name: str) -> int:
    """Submissions the run's controller has refused so far (0 if the log can't be read)."""
    r = subprocess.run(["docker", "exec", name, "grep", "-c", REFUSED,
                        "/home/slurm/simrun/log/slurmctld.log"], capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def run_dir_for(cfg: dict, workload: str, policy: str, role: str) -> Path:
    return Path(cfg["run_root"]) / workload / f"{policy}{SUFFIX[role]}"


def container_name(run_dir: Path) -> str:
    """The name sim/run_sim.sh gives the run's container."""
    return "slurmsim-" + re.sub(r"[^A-Za-z0-9_.-]", "", str(run_dir).rstrip("/").replace("/", "-"))


def image_id() -> str:
    return subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _mem_mb(text: str) -> float:
    m = re.match(r"([\d.]+)\s*([KMG]i?B|B)", text.strip())
    if not m:
        return 0.0
    scale = {"B": 1 / 2**20, "KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1,
             "GiB": 1024, "GB": 1024}[m.group(2)]
    return float(m.group(1)) * scale


class Monitor(threading.Thread):
    """Samples `docker stats` for the batch's containers: peak memory and mean CPU per run."""

    def __init__(self, names: set[str], every_s: int = 30):
        super().__init__(daemon=True)
        self.names, self.every_s = names, every_s
        self.peak_mem: dict[str, float] = {}
        self.cpu: dict[str, list[float]] = {}
        self.stopped: dict[str, str] = {}      # container -> why the watchdog stopped it
        self.first_seen: dict[str, float] = {}
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            r = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"],
                               capture_output=True, text=True)
            for line in r.stdout.splitlines():
                try:
                    name, mem, cpu = line.split("\t")
                except ValueError:
                    continue
                if name in self.names:
                    self.peak_mem[name] = max(self.peak_mem.get(name, 0.0), _mem_mb(mem.split("/")[0]))
                    self.cpu.setdefault(name, []).append(float(cpu.rstrip("%") or 0))
                    seen = self.first_seen.setdefault(name, time.time())
                    # the startup race shows in the first minutes; later logs are too big to grep
                    n = refused_submissions(name) if time.time() - seen < 900 else 0
                    if n:                       # broken run: stop it now, sim.batch retries it
                        self.stopped[name] = f"slurmctld refused {n} submission(s); stopped early"
                        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            self.stop.wait(self.every_s)


def check_run_dir(run_dir: Path, adaptive: bool) -> dict:
    from sim.check_run import KNOWN_ISSUES, check
    with _check_lock:
        out = {"problems": [], "known_issues": [], "summary_ok": None}
        try:
            out["problems"] = check(run_dir)
            out["known_issues"] = list(KNOWN_ISSUES)
            if adaptive:
                from controller.summarize import summarize
                s = summarize(run_dir)
                out["summary_ok"] = bool(s["ok"])
                if not s["ok"]:
                    out["problems"].append("adaptive summary not ok: ticks "
                                           f"{s['ticks_controller']}/{s['ticks_simulator']}, "
                                           f"failures {s['command_failures']}, applied match "
                                           f"{s['applied_matches_asked']}, replay mismatches "
                                           f"{len(s['replay_mismatches'])}")
        except Exception as exc:          # missing output, unreadable logs
            out["problems"].append(f"check failed: {exc!r}")
        return out


def log_attempt(rec: dict) -> None:
    with _log_lock:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({k: rec.get(k) for k in (
                "workload", "policy", "role", "ok", "attempt", "real_s", "peak_mem_mb",
                "cpu_pct_mean", "disk_mb", "image_id", "finished", "problems")}) + "\n")


def simulate(run_dir: Path, dtstart: int, timeout_s: float) -> tuple[int | None, str | None]:
    """Run sim/run_sim.sh; returns (exit code, problem). Stops the container on timeout."""
    try:
        proc = subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run_dir), str(dtstart)],
                              env={**os.environ, "SLURMSIM_IMAGE": IMAGE}, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", container_name(run_dir)], capture_output=True)
        return None, f"did not finish within {timeout_s / 3600:g} h of real time; stopped"
    if proc.returncode != 0:
        return proc.returncode, f"run_sim.sh exited {proc.returncode}"
    return 0, None


def run_one(cfg: dict, workload: str, policy: str, role: str, mon: Monitor | None,
            img: str) -> dict:
    run_dir = run_dir_for(cfg, workload, policy, role)
    done = ROOT / run_dir / "done.json"
    if done.exists() and json.loads(done.read_text()).get("ok"):
        return {**json.loads(done.read_text()), "skipped": True}
    pol = cfg["policies"][policy]
    scen, seed = workload.split("-s")
    src = ROOT / "workloads" / "traces" / workload
    adaptive = pol["kind"] == "adaptive"
    rec = {}
    for attempt in (1, 2):
        prepare_run(run_dir, src / "events.txt", src / "accounts.txt",
                    ROOT / "config" / "priority" / f"{pol['priority']}.conf",
                    cfg["time_start"][scen], policy="adaptive" if adaptive else None,
                    triggers=ROOT / "config" / pol.get("triggers", "triggers.yaml"))
        wait_start_slot(cfg.get("start_gap_s", 20))
        name = container_name(run_dir)
        if mon:
            mon.stopped.pop(name, None)
            mon.first_seen.pop(name, None)
        started = time.time()
        code, why = simulate(run_dir, cfg["dtstart"], cfg.get("run_timeout_h", 12) * 3600)
        real_s = time.time() - started
        if mon and name in mon.stopped:
            code, why = None, mon.stopped[name]
        chk = check_run_dir(run_dir, adaptive) if code == 0 else \
            {"problems": [why], "known_issues": [], "summary_ok": None}
        disk = sum(f.stat().st_size for f in (ROOT / run_dir).rglob("*") if f.is_file()) / 2**20
        cpu = mon.cpu.get(name, []) if mon else []
        rec = {"workload": workload, "scenario": scen, "seed": int(seed), "policy": policy,
               "role": role, "run_dir": str(run_dir), "ok": not chk["problems"],
               "attempt": attempt, "problems": chk["problems"][:20],
               "known_issues": chk["known_issues"], "summary_ok": chk["summary_ok"],
               "image": IMAGE, "image_id": img, "real_s": round(real_s, 1),
               "peak_mem_mb": round(mon.peak_mem.get(name, 0.0), 1) if mon else None,
               "cpu_pct_mean": round(sum(cpu) / len(cpu), 1) if cpu else None,
               "disk_mb": round(disk, 1),
               "finished": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        done.write_text(json.dumps(rec, indent=2) + "\n")
        log_attempt(rec)
        if rec["ok"]:
            break
        if attempt == 1:                 # keep the failed attempt; prepare_run clears run_dir
            kept = ROOT / run_dir.parent / f"{run_dir.name}.attempt1"
            if kept.exists():
                shutil.rmtree(kept)
            (ROOT / run_dir).rename(kept)
    return rec


def recheck(cfg: dict, workload: str, policy: str, role: str) -> dict | None:
    """Re-run the checks of a finished run (e.g. after a check rule changed); update done.json."""
    run_dir = run_dir_for(cfg, workload, policy, role)
    done = ROOT / run_dir / "done.json"
    if not done.exists() or not (ROOT / run_dir / "out" / "slurm_acct.out").exists():
        return None
    rec = json.loads(done.read_text())
    chk = check_run_dir(run_dir, cfg["policies"][policy]["kind"] == "adaptive")
    rec.update({"ok": not chk["problems"], "problems": chk["problems"][:20],
                "known_issues": chk["known_issues"], "summary_ok": chk["summary_ok"],
                "rechecked": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    done.write_text(json.dumps(rec, indent=2) + "\n")
    log_attempt({**rec, "finished": rec["rechecked"]})
    return rec


def main() -> int:
    cfg = yaml.safe_load(EVAL.read_text())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workloads", nargs="+", required=True)
    p.add_argument("--policies", default="all")
    p.add_argument("--role", choices=list(SUFFIX), default="matrix")
    p.add_argument("--parallel", type=int, default=9)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--recheck", action="store_true",
                   help="re-run the checks of finished runs and update their done.json")
    a = p.parse_args()
    workloads = cfg["workloads"] if a.workloads == ["all"] else a.workloads
    policies = list(cfg["policies"]) if a.policies == "all" else a.policies.split(",")
    for w in workloads:
        if not (ROOT / "workloads" / "traces" / w / "events.txt").exists():
            p.error(f"no workload {w}")
    for pol in policies:
        if pol not in cfg["policies"]:
            p.error(f"unknown policy {pol}")
    jobs = [(w, pol) for w in workloads for pol in policies]
    if a.dry_run:
        for w, pol in jobs:
            print(run_dir_for(cfg, w, pol, a.role))
        return 0
    if a.recheck:
        out = [r for w, pol in jobs if (r := recheck(cfg, w, pol, a.role))]
        for r in out:
            print(f"{r['workload']:6} {r['policy']:3} {r['role']:6} ok={r['ok']} "
                  f"known={r['known_issues']} problems={r['problems'][:2]}")
        return 0 if all(r["ok"] for r in out) else 1
    if shutil.which("docker") is None:
        p.error("docker not found")
    img = image_id()
    mon = Monitor({container_name(run_dir_for(cfg, w, pol, a.role)) for w, pol in jobs})
    mon.start()
    try:
        with ThreadPoolExecutor(max_workers=a.parallel) as ex:
            results = list(ex.map(lambda wp: run_one(cfg, wp[0], wp[1], a.role, mon, img), jobs))
    finally:
        mon.stop.set()
    for r in results:
        print(f"{r['workload']:6} {r['policy']:3} {r['role']:6} ok={r['ok']} attempt={r['attempt']} "
              f"real={r['real_s'] / 60:.0f} min mem={r.get('peak_mem_mb')} MB"
              + (" (skipped, already done)" if r.get("skipped") else "")
              + ("" if r["ok"] else f" problems={r['problems'][:2]}"))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
