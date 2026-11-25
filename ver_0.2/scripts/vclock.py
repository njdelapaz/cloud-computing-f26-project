#!/usr/bin/env python3
"""Virtual-clock tools (docker/patches/sim_determinism.patch). Standard library only.

Usage:
  vclock.py costs RUN...     fit the fixed simulated costs from real-clock runs' log/cost.csv and
                             print them as sim.conf lines (the values config/*/sim.conf.in use).
                             Make the runs with SIM_SET=ClockMode=real, since the templates use virtual
  vclock.py compare RUN...   per-run wait statistics, then for each pair of runs: whether jobcomp.log
                             is byte-identical and how many jobs differ in start, end or nodes
  vclock.py gate [REAL...]   the checks for scripts/check_vclock.sh's runs (results/vclock/); REAL
                             lists real-clock probe-day runs for the fidelity report. Exit 0 only if
                             every gating check passes
  vclock.py scale F CONF     print the cost settings of CONF (a sim.conf template) multiplied by F,
                             as Key=Value words for scripts/sim_run.sh's SIM_SET
RUN is a run directory relative to ver_0.2/ (e.g. results/vclock/cal_probe_real).

Gate checks (all runs use ClockMode=virtual with the template's costs):
  1. repeats: two identical runs give a byte-identical jobcomp.log, on the smoke trace, the
     contended trace, the probe day and two B1 samples (2024-01-30 seed 2, 2024-02-12 seed 3)
  2. G1: priority/adaptive with DefaultPolicy=BASE (JobSize-only weights in slurm.conf) gives every
     job the same start, end and nodes as stock with BASE's weights, on the smoke trace, the
     contended trace and the probe day; each adaptive run logs that it loaded BASE
  3. controls: stock with JobSize-only weights differs from BASE on the smoke and contended traces,
     so the matches in 2 aren't an accident of the traces
  4. costs: both templates set every cost key to the same values, not all zero, and the
     virtual probe-day run charged them
Not gating (INFO): the probe day on the virtual clock next to real-clock runs of it (fidelity);
the probe day with every cost halved and doubled, when check_vclock.sh ran with SENS=1; real time.

Cost model (charged only with ClockMode=virtual; times in usec):
  main scheduler pass  SchedPassCost + SchedJobCost x jobs tested   least squares over all passes
  backfill cycle       BfCycleCost + BfJobCost x jobs tested        least squares; yields excluded
  job submission       SubmitCost                                    mean
  job-complete or epilog-complete event that ends a job  CompleteCost  mean (ignored repeats excluded)
  _slurmctld_background call  BackgroundCost                         mean
  priority decay run   DecayCost                                     mean
Least squares and means keep the total simulated time the scheduler spends equal to the real-clock
runs' total, which is what the sub-second phase of every later event depends on. If least squares
gives a negative intercept, the line goes through 0 with slope total time / total jobs tested, which
keeps the total.
"""
import csv
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
COST_KEYS = ("SchedPassCost", "SchedJobCost", "BfCycleCost", "BfJobCost", "SubmitCost",
             "CompleteCost", "BackgroundCost", "DecayCost")
KV = re.compile(r"(\w+)=(\S*)")
COMPLETE_KINDS = ("complete", "epilog")


def cost_rows(run):
    path = VER02 / run / "log" / "cost.csv"
    if not path.exists():
        raise SystemExit(f"missing {path.relative_to(VER02)}")
    rows = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if None in r.values():      # a line cut off at the end of the run
                continue
            rows[r["kind"]].append((int(r["n"]), int(r["real_usec"])))
    return rows


def fit(points):
    """Least-squares a + b*n with a, b >= 0 and the total kept; returns (a, b, r2)."""
    ns = [p[0] for p in points]
    ys = [p[1] for p in points]
    mn, my = statistics.fmean(ns), statistics.fmean(ys)
    var = sum((n - mn) ** 2 for n in ns)
    b = sum((n - mn) * (y - my) for n, y in points) / var if var else 0.0
    if b < 0:
        a, b = my, 0.0
    elif my - b * mn < 0:
        a, b = 0.0, my / mn
    else:
        a = my - b * mn
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - a - b * n) ** 2 for n, y in points)
    return a, b, (1 - ss_res / ss_tot) if ss_tot else 1.0


def costs(runs):
    rows = defaultdict(list)
    for run in runs:
        for k, v in cost_rows(run).items():
            rows[k].extend(v)
    print(f"# Real-clock cost samples from {len(runs)} run(s): {', '.join(runs)}")
    print(f"# {'kind':<12} {'count':>8} {'mean n':>8} {'mean us':>9} {'median us':>9} {'p99 us':>9} {'max us':>9}")
    for k in sorted(rows):
        ys = sorted(p[1] for p in rows[k])
        ns = [p[0] for p in rows[k]]
        print(f"# {k:<12} {len(ys):>8} {statistics.fmean(ns):>8.1f} {statistics.fmean(ys):>9.1f} "
              f"{ys[len(ys) // 2]:>9} {ys[int(len(ys) * 0.99)]:>9} {ys[-1]:>9}")
    out = {}
    for kind, (fixed, per_job) in {"sched": ("SchedPassCost", "SchedJobCost"),
                                   "bf": ("BfCycleCost", "BfJobCost")}.items():
        if rows[kind]:
            a, b, r2 = fit(rows[kind])
            out[fixed], out[per_job] = a, b
            print(f"# {kind}: {a:.1f} us + {b:.2f} us/job tested (R^2 {r2:.2f}, {len(rows[kind])} samples)")
    if rows["submit"]:
        out["SubmitCost"] = statistics.fmean(p[1] for p in rows["submit"])
    done = [p[1] for k in COMPLETE_KINDS for p in rows[k]]
    if done:
        out["CompleteCost"] = statistics.fmean(done)
    if rows["background"]:
        out["BackgroundCost"] = statistics.fmean(p[1] for p in rows["background"])
    if rows["decay"]:
        out["DecayCost"] = statistics.fmean(p[1] for p in rows["decay"])
    print("ClockMode = virtual")
    for k in COST_KEYS:
        if k in out:
            print(f"{k} = {out[k] / 1e6:.6f}")


def jobs(run):
    path = VER02 / run / "log" / "jobcomp.log"
    out = {}
    for line in path.read_text().splitlines():
        if line.startswith("JobId="):
            d = dict(KV.findall(line))
            ts = lambda key: datetime.fromisoformat(d[key]).timestamp()  # noqa: E731
            out[d["Name"]] = (ts("StartTime"), ts("EndTime"), d["NodeList"], ts("SubmitTime"))
    return out


def real_seconds(run):
    m = re.search(r"Real time: (\d+):(\d+):([\d.]+)", (VER02 / run / "log" / "slurmsim.log").read_text())
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else float("nan")


def compare(runs):
    print(f"{'run':<40} {'jobs':>5} {'mean':>6} {'median':>6} {'p95':>6} {'max':>7} {'>60s':>5} {'real s':>7}")
    js = {}
    for run in runs:
        j = js[run] = jobs(run)
        w = sorted(x[0] - x[3] for x in j.values())
        print(f"{run:<40} {len(j):>5} {statistics.fmean(w):>6.0f} {w[len(w) // 2]:>6.0f} "
              f"{w[int(len(w) * 0.95)]:>6.0f} {w[-1]:>7.0f} {sum(x > 60 for x in w):>5} {real_seconds(run):>7.0f}")
    for a, b in combinations(runs, 2):
        same_bytes = (VER02 / a / "log" / "jobcomp.log").read_bytes() == (VER02 / b / "log" / "jobcomp.log").read_bytes()
        d = [n for n in set(js[a]) | set(js[b]) if (js[a].get(n) or ())[:3] != (js[b].get(n) or ())[:3]]
        print(f"{a} vs {b}: jobcomp.log {'byte-identical' if same_bytes else 'differs'}; "
              f"{len(d)} of {len(set(js[a]) | set(js[b]))} jobs differ in start, end or nodes")


def template_costs(conf):
    out = {}
    for line in (VER02 / conf).read_text().splitlines():
        m = re.match(r"\s*(\w+)\s*=\s*([0-9.eE+-]+)\s*$", line)
        if m and m[1] in COST_KEYS:
            out[m[1]] = float(m[2])
    return out


def scale(args):
    factor, conf = float(args[0]), args[1]
    print(" ".join(f"{k}={v * factor:.6f}" for k, v in template_costs(conf).items()))


def wait_stats(js):
    w = sorted(x[0] - x[3] for x in js.values())
    return {"jobs": len(w), "mean": statistics.fmean(w), "median": w[len(w) // 2],
            "p95": w[int(len(w) * 0.95)], ">60s": sum(x > 60 for x in w)}


def gate(real_runs):
    R = "results/vclock/"
    results = []

    def check(label, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: {detail}")

    def have(run):
        return (VER02 / R / run / "log" / "jobcomp.log").exists()

    # 1. repeats
    pairs = [("smoke", "sm"), ("contended", "ct"), ("probe day", "pr"), ("2024-01-30 s2", "s0130"),
             ("2024-02-12 s3", "s0212")]
    parts, ok = [], True
    for label, pre in pairs:
        a, b = f"{pre}_stock_1", f"{pre}_stock_2"
        if not (have(a) and have(b)):
            parts.append(f"{label} missing"); ok = False; continue
        ja, jb = jobs(R + a), jobs(R + b)
        same = (VER02 / R / a / "log/jobcomp.log").read_bytes() == (VER02 / R / b / "log/jobcomp.log").read_bytes()
        d = sum(1 for n in set(ja) | set(jb) if (ja.get(n) or ())[:3] != (jb.get(n) or ())[:3])
        parts.append(f"{label} {len(ja)} jobs {'identical' if same else f'DIFFER ({d} jobs)'}")
        ok &= same
    check("1 repeats byte-identical", ok, "; ".join(parts))

    # 2. G1 on the virtual clock
    parts, ok = [], True
    for label, pre in pairs[:3]:
        s, a = f"{pre}_stock_1", f"{pre}_adaptive"
        if not (have(s) and have(a)):
            parts.append(f"{label} missing"); ok = False; continue
        js, ja = jobs(R + s), jobs(R + a)
        d = sum(1 for n in set(js) | set(ja) if (js.get(n) or ())[:3] != (ja.get(n) or ())[:3])
        log = (VER02 / R / a / "log" / "slurmctld.log").read_text(errors="replace")
        base = "adaptive: loaded policy=BASE" in log
        parts.append(f"{label} {d} of {len(js)} jobs differ{'' if base else ', BASE NOT LOADED'}")
        ok &= d == 0 and base
    check("2 adaptive BASE == stock (start, end, nodes)", ok, "; ".join(parts))

    # 3. controls
    parts, ok = [], True
    for label, pre in pairs[:2]:
        s, c = f"{pre}_stock_1", f"{pre}_size"
        if not (have(s) and have(c)):
            parts.append(f"{label} missing"); ok = False; continue
        js, jc = jobs(R + s), jobs(R + c)
        d = sum(1 for n in set(js) | set(jc) if (js.get(n) or ())[:3] != (jc.get(n) or ())[:3])
        parts.append(f"{label} {d} of {len(js)} jobs differ")
        ok &= d > 0
    check("3 JobSize-only control differs from BASE", ok, "; ".join(parts))

    # 4. costs configured and charged
    tc = {c: template_costs(f"config/{c}/sim.conf.in") for c in ("smoke", "project")}
    charged = 0
    if have("pr_stock_1"):
        for r in csv.DictReader(open(VER02 / R / "pr_stock_1" / "log" / "cost.csv", newline="")):
            if r["kind"] == "sched" and r["charged_usec"]:
                charged += int(r["charged_usec"])
    ok = (all(k in tc[c] for c in tc for k in COST_KEYS) and tc["smoke"] == tc["project"]
          and sum(tc["project"].values()) > 0 and charged > 0)
    check("4 costs set and charged", ok,
          f"project template {', '.join(f'{k}={v * 1e6:.1f}us' for k, v in tc['project'].items())}; "
          f"probe day charged {charged / 1e6:.1f} s to main-scheduler passes")

    # INFO: fidelity, sensitivity, real time
    rows = [r for r in ["pr_stock_1", "pr_costs_half", "pr_costs_double"] if have(r)]
    print("\nINFO probe day, virtual clock vs real clock (waits in s):")
    print(f"  {'run':<38} {'jobs':>5} {'mean':>6} {'median':>6} {'p95':>6} {'>60s':>5} {'real s':>7}")
    stats = {}
    for run in [R + r for r in rows] + list(real_runs):
        st = stats[run] = wait_stats(jobs(run))
        print(f"  {run:<38} {st['jobs']:>5} {st['mean']:>6.0f} {st['median']:>6.0f} {st['p95']:>6.0f} "
              f"{st['>60s']:>5} {real_seconds(run):>7.0f}")
    if real_runs and R + "pr_stock_1" in stats:
        v = stats[R + "pr_stock_1"]
        inside = [k for k in ("mean", "median", "p95", ">60s")
                  if min(stats[r][k] for r in real_runs) <= v[k] <= max(stats[r][k] for r in real_runs)]
        print(f"  virtual run inside the real-clock range on: {', '.join(inside) or 'none'} "
              f"(of mean, median, p95, >60s; {len(real_runs)} real runs)")
    print("\nINFO real time per run (s): " + ", ".join(
        f"{r} {real_seconds(R + r):.0f}" for r in sorted(p.name for p in (VER02 / R).iterdir()
                                                       if p.is_dir() and have(p.name) and p.name[:3] in ("sm_", "ct_", "pr_", "s01", "s02"))))
    print(f"\n{sum(results)} of {len(results)} gating checks pass")
    sys.exit(0 if all(results) else 1)


def main():
    cmds = {"costs": costs, "compare": compare, "gate": gate, "scale": scale}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds or (sys.argv[1] != "gate" and len(sys.argv) < 3):
        raise SystemExit(__doc__)
    cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
