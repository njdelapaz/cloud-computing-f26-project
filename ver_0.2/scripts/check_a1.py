#!/usr/bin/env python3
"""Check the A1 runs made by scripts/check_a1.sh. Standard library only.

Usage:
  check_a1.py same RUN_A RUN_B        exit 0 if the two runs (under results/a1/) give every job the
                                      same start time, end time and nodes; prints the first differences
  check_a1.py report [--reruns NAMES] print the gate table; exit 0 only if every gating check passes.
                                      NAMES lists the pairs that had to be rerun (comma-separated)

Gate checks (WORKFLOW A1, SPEC G1):
  1. smoke trace: stock multifactor with BASE's weights in slurm.conf, and priority/adaptive with
     DefaultPolicy=BASE and JobSize-only weights in slurm.conf, give identical start times, end
     times and nodes. A stock run with the JobSize-only weights must differ, so the match isn't
     an accident of the trace.
  2. contended trace (config/smoke/contended.events): a queue forms early, so ordering decides
     most starts. The simulator's clock runs at real speed while slurmctld works, so repeated
     runs can differ by 1 s here and there. Three stock repeats must agree within 1 s (same
     nodes, starts and ends within 1 s, no start-order flips); adaptive BASE must agree with them
     by the same rule; the JobSize-only stock control must not.
  3. the weights come from the policy: FixedPolicy=U (Age only) on the smoke trace gives
     submission order, although slurm.conf still says JobSize only.
  4. a malformed policies.conf stops slurmctld with a clear fatal error.
  5. the parser unit tests pass (plugin/tests/test_adaptive_conf.py, run in the image).
Not gating (INFO): the one-day probe trace, when its runs are present (check_a1.sh PROBE=1).
On that trace the 1 s jitter snowballs, so even identical stock runs differ on most jobs; the
line reports that spread next to the stock/adaptive difference.
"""
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "results" / "a1"
KV = re.compile(r"(\w+)=(\S*)")
SMOKE_PENDING = [f"jobid_{i}" for i in range(1002, 1008)]
CONTENDED_JOBS = 41
PROBE_JOBS = 3028
TOL = 1  # seconds of clock jitter allowed by the contended check


def jobs(run, must_exist=True):
    path = ROOT / run / "log" / "jobcomp.log"
    if not path.exists():
        if must_exist:
            raise SystemExit(f"missing {path}")
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.startswith("JobId="):
            d = dict(KV.findall(line))
            out[d["Name"]] = (datetime.fromisoformat(d["StartTime"]).timestamp(),
                              datetime.fromisoformat(d["EndTime"]).timestamp(), d["NodeList"],
                              datetime.fromisoformat(d["SubmitTime"]).timestamp())
    return out


def diff(a, b):
    """Jobs whose (start, end, nodes) differ, including jobs present in only one run."""
    return sorted(n for n in set(a) | set(b) if (a.get(n) or ())[:3] != (b.get(n) or ())[:3])


def agree(a, b, tol=TOL):
    """How far run b is from run a once tol seconds of clock jitter are allowed.

    Returns (jobs missing from either run, jobs on different nodes, jobs whose start or end moved
    more than tol, start-order flips: pairs that start more than tol apart in both runs, in
    opposite orders).
    """
    missing = len(set(a) ^ set(b))
    common = sorted(set(a) & set(b), key=lambda n: a[n][0])
    nodes = sum(1 for n in common if a[n][2] != b[n][2])
    moved = sum(1 for n in common if abs(a[n][0] - b[n][0]) > tol or abs(a[n][1] - b[n][1]) > tol)
    flips = sum(1 for i, x in enumerate(common) for y in common[i + 1:]
                if a[y][0] - a[x][0] > tol and b[x][0] - b[y][0] > tol)
    return missing, nodes, moved, flips


def mean_wait(js):
    return statistics.mean(j[0] - j[3] for j in js.values())


def start_order(js, names):
    return sorted(names, key=lambda n: (js[n][0], n))


def ctld_log(run):
    return (ROOT / run / "log" / "slurmctld.log").read_text(errors="replace")


def slurm_weights(run):
    conf = (ROOT / run / "etc" / "slurm.conf").read_text()
    w = dict(re.findall(r"^PriorityWeight(\w+)=(\d+)", conf, re.M))
    return " ".join(f"{k}={w[k]}" for k in ("Age", "Fairshare", "JobSize") if k in w)


def loaded(run):
    m = re.search(r"adaptive: loaded policy=(\S+) mode=(\S+) Fairshare=(\d+) Age=(\d+) JobSize=(\d+)",
                  ctld_log(run))
    return m.groups() if m else None


def same(a, b):
    ja, jb = jobs(a), jobs(b)
    d = diff(ja, jb)
    if d:
        print(f"{a} vs {b}: {len(d)} of {len(set(ja) | set(jb))} jobs differ, e.g.")
        for n in d[:5]:
            print(f"  {n}: {ja.get(n)} vs {jb.get(n)}")
    return not d


def report(reruns):
    results = []
    BASE = ("BASE", "DEFAULT", "70000", "20000", "10000")

    def check(label, ok, detail, gating=True):
        results.append((label, ok, detail, gating))

    def note(pair):
        return " (pair rerun once)" if pair in reruns else ""

    # 1. smoke: stock BASE weights vs adaptive with DefaultPolicy=BASE, exact
    stock, adapt, size = jobs("smoke_stock_base"), jobs("smoke_adaptive_base"), jobs("smoke_stock_size")
    got = loaded("smoke_adaptive_base")
    d = diff(stock, adapt)
    nontrivial = start_order(size, SMOKE_PENDING) != start_order(stock, SMOKE_PENDING)
    check("smoke: adaptive BASE == stock multifactor",
          not d and len(stock) == 7 and got == BASE and nontrivial,
          f"{len(stock)} jobs, {len(d)} differ; plugin loaded {got and got[0]} with Fairshare/Age/JobSize "
          f"{got and '/'.join(got[2:])} while slurm.conf said {slurm_weights('smoke_adaptive_base')}; "
          f"order {' '.join(n[-4:] for n in start_order(stock, SMOKE_PENDING))} vs "
          f"{' '.join(n[-4:] for n in start_order(size, SMOKE_PENDING))} under slurm.conf's weights"
          f"{note('smoke')}")

    # 2. contended trace: agreement within the simulator's 1 s jitter
    ref = jobs("contended_stock_base_1")
    others = {r: agree(ref, jobs(r)) for r in
              ("contended_stock_base_2", "contended_stock_base_3", "contended_adaptive_base")}
    control = agree(ref, jobs("contended_stock_size"))
    got = loaded("contended_adaptive_base")
    exact = len(diff(ref, jobs("contended_adaptive_base")))
    waited = sum(1 for j in ref.values() if j[0] - j[3] > 60)
    check(f"contended: adaptive BASE == stock within {TOL} s",
          len(ref) == CONTENDED_JOBS and all(v == (0, 0, 0, 0) for v in others.values())
          and got == BASE and control[3] > 0,
          f"{len(ref)} jobs, {waited} waited over 60 s; stock repeats 2 and 3 and adaptive vs stock 1: "
          + ", ".join(f"{v[1]}/{v[2]}/{v[3]}" for v in others.values())
          + f" (nodes differ / moved > {TOL} s / order flips); adaptive exact-match differences {exact}, "
          f"all within {TOL} s; JobSize-only control {control[1]}/{control[2]}/{control[3]}")

    # 3. weights really come from the policy
    fixed = jobs("smoke_adaptive_fixed_u")
    got = loaded("smoke_adaptive_fixed_u")
    order = start_order(fixed, SMOKE_PENDING)
    check("policy weights used: FixedPolicy=U gives submission order",
          order == SMOKE_PENDING and got == ("U", "FIXED", "0", "100000", "0"),
          f"plugin loaded {got and got[0]} ({got and got[1]}) over DefaultPolicy=BASE; order "
          f"{' '.join(n[-4:] for n in order)} with slurm.conf {slurm_weights('smoke_adaptive_fixed_u')}")

    # 4. malformed file -> fatal
    log = ctld_log("smoke_adaptive_bad")
    m = re.search(r"fatal: adaptive: (.*)", log)
    ran = len(jobs("smoke_adaptive_bad", False))
    check("malformed policies.conf is fatal",
          bool(m) and "Fairshare+Age+JobSize" in m.group(1) and not ran,
          (m.group(1).strip() if m else "no fatal message in slurmctld.log") + f"; {ran} jobs ran")

    # 5. parser unit tests
    ut = (ROOT / "unit_tests.txt").read_text()
    m = re.search(r"Ran (\d+) tests", ut)
    check("parser unit tests", bool(m) and ut.rstrip().endswith("OK"),
          f"{m.group(1) if m else '?'} tests, {ut.rstrip().splitlines()[-1] if ut.strip() else 'no output'}")

    # INFO: one-day probe trace, if its runs are present
    probe = [r for r in ("probe_stock_base", "probe_stock_base_2", "probe_adaptive_base", "probe_adaptive_base_2")
             if (ROOT / r / "log" / "jobcomp.log").exists()]
    if "probe_stock_base" in probe and len(probe) > 1:
        ref = jobs("probe_stock_base")
        parts = [f"{r.replace('probe_', '')}: {len(diff(ref, jobs(r)))} differ, mean wait {mean_wait(jobs(r)):.0f} s"
                 for r in probe[1:]]
        check("probe day (not gating): run-to-run spread", True,
              f"vs stock_base ({len(ref)} jobs, mean wait {mean_wait(ref):.0f} s): " + "; ".join(parts),
              gating=False)

    width = max(len(r[0]) for r in results)
    for label, ok, detail, gating in results:
        tag = ("PASS" if ok else "FAIL") if gating else "INFO"
        print(f"{tag}  {label:<{width}}  {detail}")
    return all(ok for _, ok, _, gating in results if gating)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "same":
        sys.exit(0 if same(sys.argv[2], sys.argv[3]) else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "report":
        reruns = sys.argv[3].split(",") if len(sys.argv) == 4 and sys.argv[2] == "--reruns" else []
        sys.exit(0 if report(reruns) else 1)
    raise SystemExit(__doc__)
