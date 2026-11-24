#!/usr/bin/env python3
"""Check the A1 runs made by scripts/check_a1.sh. Standard library only.

Usage:
  check_a1.py same RUN_A RUN_B        exit 0 if the two runs (under results/a1/) give every job the
                                      same start time, end time and nodes; prints the first differences
  check_a1.py report [--reruns NAMES] print the gate table; exit 0 only if every check passes.
                                      NAMES lists the pairs that had to be rerun (comma-separated)

Gate checks (WORKFLOW A1, SPEC G1):
  1. smoke trace: stock multifactor with BASE's weights in slurm.conf, and priority/adaptive with
     DefaultPolicy=BASE and JobSize-only weights in slurm.conf, give identical start times, end
     times and nodes. A stock run with the JobSize-only weights must differ, so the match isn't
     an accident of the trace.
  2. the same comparison on the one-day probe trace (16-node project cluster).
  3. the weights come from the policy: FixedPolicy=U (Age only) on the smoke trace gives
     submission order, although slurm.conf still says JobSize only.
  4. a malformed policies.conf stops slurmctld with a clear fatal error.
  5. the parser unit tests pass (plugin/tests/test_adaptive_conf.py, run in the image).
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "results" / "a1"
KV = re.compile(r"(\w+)=(\S*)")
SMOKE_PENDING = [f"jobid_{i}" for i in range(1002, 1008)]


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
            out[d["Name"]] = (datetime.fromisoformat(d["StartTime"]),
                              datetime.fromisoformat(d["EndTime"]), d["NodeList"])
    return out


def diff(a, b):
    """Jobs whose (start, end, nodes) differ, including jobs present in only one run."""
    return sorted(n for n in set(a) | set(b) if a.get(n) != b.get(n))


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


def real_time(run):
    m = re.search(r"Real time: (\S+)", (ROOT / run / "log" / "slurmsim.log").read_text(errors="replace"))
    return m.group(1).split(".")[0] if m else "?"


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

    def check(label, ok, detail):
        results.append((label, ok, detail))

    def note(pair):
        return " (pair rerun once)" if pair in reruns else ""

    # 1. smoke: stock BASE weights vs adaptive with DefaultPolicy=BASE
    stock, adapt, size = jobs("smoke_stock_base"), jobs("smoke_adaptive_base"), jobs("smoke_stock_size")
    got = loaded("smoke_adaptive_base")
    d = diff(stock, adapt)
    nontrivial = start_order(size, SMOKE_PENDING) != start_order(stock, SMOKE_PENDING)
    check("smoke: adaptive BASE == stock multifactor",
          not d and len(stock) == 7 and got == ("BASE", "DEFAULT", "70000", "20000", "10000") and nontrivial,
          f"{len(stock)} jobs, {len(d)} differ; plugin loaded {got and got[0]} with Fairshare/Age/JobSize "
          f"{got and '/'.join(got[2:])} while slurm.conf said {slurm_weights('smoke_adaptive_base')}; "
          f"order {' '.join(n[-4:] for n in start_order(stock, SMOKE_PENDING))} vs "
          f"{' '.join(n[-4:] for n in start_order(size, SMOKE_PENDING))} under slurm.conf's weights"
          f"{note('smoke')}")

    # 2. one-day probe trace on the project cluster
    if (ROOT / "probe_adaptive_base" / "log" / "jobcomp.log").exists():
        stock, adapt = jobs("probe_stock_base"), jobs("probe_adaptive_base")
        got = loaded("probe_adaptive_base")
        d = diff(stock, adapt)
        check("probe day: adaptive BASE == stock multifactor",
              not d and len(stock) == 3028 and got and got[0] == "BASE",
              f"{len(stock)} jobs, {len(d)} differ; plugin loaded {got and got[0]}, slurm.conf said "
              f"{slurm_weights('probe_adaptive_base')}; real time {real_time('probe_stock_base')} stock, "
              f"{real_time('probe_adaptive_base')} adaptive{note('probe')}")
    else:
        check("probe day: adaptive BASE == stock multifactor", False, "not run (SKIP_PROBE=1)")

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
    check("malformed policies.conf is fatal",
          bool(m) and "Fairshare+Age+JobSize" in m.group(1) and not jobs("smoke_adaptive_bad", False),
          (m.group(1).strip() if m else "no fatal message in slurmctld.log")
          + f"; {len(jobs('smoke_adaptive_bad', False))} jobs ran")

    # 5. parser unit tests
    ut = (ROOT / "unit_tests.txt").read_text()
    m = re.search(r"Ran (\d+) tests", ut)
    check("parser unit tests", bool(m) and ut.rstrip().endswith("OK"),
          f"{m.group(1) if m else '?'} tests, {ut.rstrip().splitlines()[-1] if ut.strip() else 'no output'}")

    width = max(len(r[0]) for r in results)
    for label, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label:<{width}}  {detail}")
    return all(ok for _, ok, _ in results)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "same":
        sys.exit(0 if same(sys.argv[2], sys.argv[3]) else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "report":
        reruns = sys.argv[3].split(",") if len(sys.argv) == 4 and sys.argv[2] == "--reruns" else []
        sys.exit(0 if report(reruns) else 1)
    raise SystemExit(__doc__)
