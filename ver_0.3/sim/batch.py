"""Task 3.2: run a batch of Phase 3 runs (SPEC §8.4) and check each one.

Usage:
  .venv/bin/python -m sim.batch --workloads N-s1 [X-s1 ...|all] [--policies all|A1,C0]
                                [--role matrix|pilot|repeat] [--parallel 9] [--dry-run]

Policies, workloads, start times and the run folder come from config/evaluation.yaml. A run
of policy P on workload W goes to <run_root>/W/P (role matrix), W/P-pilot or W/P-repeat.

For each run: prepare (sim.prepare_run), simulate (sim/run_sim.sh with the slurmsim:v03
image), then check: sim.check_run for every run, and controller.summarize for adaptive runs
(every tick logged on both sides, every switch applied, replay exact). A run that fails is
rerun once. Each run folder gets done.json (outcome, attempt, image id, real time, peak
container memory, mean CPU, disk use); a run whose done.json says ok is skipped, so a batch
can be resumed. One line per finished run is appended to results/phase3/batches.jsonl.
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
        started = time.time()
        proc = subprocess.run([str(ROOT / "sim" / "run_sim.sh"), str(run_dir), str(cfg["dtstart"])],
                              env={**os.environ, "SLURMSIM_IMAGE": IMAGE})
        real_s = time.time() - started
        chk = check_run_dir(run_dir, adaptive) if proc.returncode == 0 else \
            {"problems": [f"run_sim.sh exited {proc.returncode}"], "known_issues": [], "summary_ok": None}
        name = container_name(run_dir)
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
        if rec["ok"]:
            break
    with _log_lock:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({k: rec[k] for k in ("workload", "policy", "role", "ok", "attempt",
                                                    "real_s", "peak_mem_mb", "cpu_pct_mean",
                                                    "disk_mb", "image_id", "finished")}) + "\n")
    return rec


def main() -> int:
    cfg = yaml.safe_load(EVAL.read_text())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workloads", nargs="+", required=True)
    p.add_argument("--policies", default="all")
    p.add_argument("--role", choices=list(SUFFIX), default="matrix")
    p.add_argument("--parallel", type=int, default=9)
    p.add_argument("--dry-run", action="store_true")
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
