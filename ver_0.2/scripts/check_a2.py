#!/usr/bin/env python3
"""A2 gate report for scripts/check_a2.sh's runs (results/a2/). Standard library only.

Gating checks (all runs on the virtual clock):
  1. unit tests: the decide() table (plugin/tests/test_decide.py), the parser
     (test_adaptive_conf.py), the shared policy state (test_adaptive_policy.py) and tests/ pass
  2. G1: with BASE and no windows, priority/adaptive gives every job the same start, end and nodes
     as stock multifactor on the smoke trace, the contended trace and the probe day; its decision
     log is valid, has one row per controller tick, is AUTO throughout and never switches
  3. G2: on the contended trace with config/smoke/schedule_g2.conf, the decision log is OVERRIDE
     from the first tick at or after each window's start, and AUTO again from the first tick at or
     after its end, each within one tick, with a switch at both ends
  4. hot swap: the switch into the one-off window recomputed the pending jobs' priorities, and every
     job that started inside that window started at the same time, on the same nodes, as in the
     static FixedPolicy=U run; the run as a whole differs from adaptive BASE
  5. cooldown: after each window ends, AUTO isn't eligible until TMin (10 min) has passed, and no
     tick samples while it isn't (validate.py with --tmin checks every row; this counts them)
  6. FIXED and FORCED in the plugin: FixedPolicy=U is FIXED on every tick and never switches;
     ForceAlternate=F,U with ForcePeriod=1min is FORCED, switches on every tick after the first,
     never samples, and recomputes priorities on every switch
Not gating (INFO): swap_us against pending jobs, and the controller's real cost per tick from
log/cost.csv's decay rows next to the fixed DecayCost, for the sim_charge decision (SPEC §8).
"""
import csv
import io
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02 / "scripts"))
import validate as v  # noqa: E402

R = VER02 / "results" / "a2"
KV = re.compile(r"(\w+)=(\S*)")
TICK = 60
G2_TMIN = 600
T0 = 1704067200                     # config/smoke runs start at 2024-01-01T00:00:00Z
NAMES = set(v.parse_policies((VER02 / "config" / "policies.conf").read_text())[0])
results = []


def check(label, ok, detail, gating=True):
    if gating:
        results.append(ok)
    print(f"[{('PASS' if ok else 'FAIL') if gating else 'INFO'}] {label}: {detail}")


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def hm(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


def have(run):
    return (R / run / "log" / "jobcomp.log").exists()


def jobs(run):
    out = {}
    for line in (R / run / "log" / "jobcomp.log").read_text().splitlines():
        if line.startswith("JobId="):
            d = dict(KV.findall(line))
            out[d["Name"]] = (utc(d["StartTime"]), utc(d["EndTime"]), d["NodeList"])
    return out


def differ(a, b):
    return sorted(n for n in set(a) | set(b) if a.get(n) != b.get(n))


def decisions(run):
    path = R / run / "log" / "decisions.csv"
    if not path.exists():
        return None, []
    text = path.read_text()
    return text, list(csv.DictReader(io.StringIO(text)))


def ctld_log(run):
    path = R / run / "log" / "slurmctld.log"
    return path.read_text(errors="replace") if path.exists() else ""


def cost(run, kind):
    path = R / run / "log" / "cost.csv"
    if not path.exists():
        return []
    return [int(r["real_usec"]) for r in csv.DictReader(path.open()) if r["kind"] == kind]


def windows_in(rows):
    """[(start, end, policy)] of config/smoke/schedule_g2.conf's windows within the run"""
    t_first, t_last = int(rows[0]["sim_time"]), int(rows[-1]["sim_time"])
    out = []
    for _, line in v._lines((VER02 / "config" / "smoke" / "schedule_g2.conf").read_text()):
        kv = dict(tok.split("=", 1) for tok in line.split()[1:])
        if "Daily" in kv:
            a, b = (v._hhmm(x) * 60 for x in kv["Daily"].split("-"))
            day = t_first - t_first % 86400
            while day <= t_last:
                out.append((day + a, day + b, kv["Policy"]))
                day += 86400
        else:
            out.append((v._ts(kv["Start"]), v._ts(kv["End"]), kv["Policy"]))
    return sorted(w for w in out if w[0] <= t_last and w[1] > t_first)


def first_at(rows, t):
    return next((r for r in rows if int(r["sim_time"]) >= t), None)


def report():
    # 1. unit tests
    ut = (R / "unit_tests.txt").read_text() if (R / "unit_tests.txt").exists() else ""
    parts, ok = [], True
    for sec in re.split(r"^== ", ut, flags=re.M)[1:]:
        name = sec.splitlines()[0].strip()
        m = re.search(r"^Ran (\d+) tests?", sec, re.M)
        good = bool(m) and bool(re.search(r"^OK\b", sec, re.M)) and "FAILED" not in sec
        label = name.split()[0].rstrip("/").rsplit("/", 1)[-1]
        parts.append(f"{label} {m.group(1) if m else '?'} {'OK' if good else 'FAILED'}")
        ok &= good
    check("1 unit tests", ok and len(parts) == 4, "; ".join(parts) or "no results/a2/unit_tests.txt")

    # 2. G1
    parts, ok = [], True
    for label, pre in (("smoke", "sm"), ("contended", "ct"), ("probe day", "pr")):
        s, a = f"{pre}_stock", f"{pre}_adaptive"
        if not (have(s) and have(a)):
            parts.append(f"{label} missing")
            ok = False
            continue
        js, ja = jobs(s), jobs(a)
        d = differ(js, ja)
        text, rows = decisions(a)
        errs = v.validate_decisions(text or "", NAMES, 3600, TICK)
        ticks = len(cost(a, "decay"))
        modes = {r["mode"] for r in rows}
        switches = sum(r["switched"] == "1" for r in rows)
        base = "adaptive: loaded policy=BASE" in ctld_log(a)
        good = not d and base and not errs and rows and len(rows) == ticks and modes == {"AUTO"} \
            and not switches
        parts.append(f"{label} {len(d)} of {len(js)} jobs differ, {len(rows)} log rows for {ticks} ticks, "
                     f"modes {'/'.join(sorted(modes))}, {switches} switches"
                     + ("" if base else ", BASE NOT LOADED") + (f", log invalid: {errs[0]}" if errs else ""))
        ok &= bool(good)
    check("2 G1: adaptive BASE, no windows == stock (start, end, nodes)", ok, "; ".join(parts))

    # 3. G2 boundaries
    text, rows = decisions("g2_window")
    if not rows:
        check("3 G2: override windows switch on time", False, "no results/a2/g2_window/log/decisions.csv")
        return
    errs = v.validate_decisions(text, NAMES, G2_TMIN, TICK)
    wins = windows_in(rows)
    parts, ok = [], not errs and len(wins) == 2
    for s, e, pol in wins:
        on, off = first_at(rows, s), first_at(rows, e)
        inside = [r for r in rows if s <= int(r["sim_time"]) < e]
        good = (on is not None and off is not None and on["mode"] == "OVERRIDE" and on["switched"] == "1"
                and on["chosen_policy"] == pol and 0 <= int(on["sim_time"]) - s < TICK
                and off["mode"] == "AUTO" and off["switched"] == "1" and off["chosen_policy"] == "BASE"
                and 0 <= int(off["sim_time"]) - e < TICK
                and all(r["mode"] == "OVERRIDE" and r["chosen_policy"] == pol for r in inside))
        outside = [r for r in rows if not any(a <= int(r["sim_time"]) < b for a, b, _ in wins)]
        good &= all(r["mode"] == "AUTO" for r in outside)
        parts.append(f"[{hm(s)}, {hm(e)}) -> {pol}: OVERRIDE from {hm(int(on['sim_time'])) if on else '?'} "
                     f"(+{int(on['sim_time']) - s if on else '?'} s), AUTO from "
                     f"{hm(int(off['sim_time'])) if off else '?'} (+{int(off['sim_time']) - e if off else '?'} s)")
        ok &= good
    check("3 G2: windows switch within one tick", ok,
          "; ".join(parts) + f"; {len(rows)} ticks, log {'valid' if not errs else 'INVALID: ' + errs[0]}")

    # 4. hot swap effect
    s, e, pol = wins[0]
    on = first_at(rows, s)
    m = re.search(rf"adaptive: switch BASE -> {re.escape(pol)} at sim_time={on['sim_time']} \(OVERRIDE\): "
                  r"ordering weights changed, priorities recomputed, (\d+) pending jobs, swap_us=(\d+)",
                  ctld_log("g2_window"))
    jw, ju, jb = jobs("g2_window"), jobs("ct_fixed_u"), jobs("ct_adaptive")
    started = sorted(n for n, (st, _, _) in jw.items() if s <= st < e)
    same_u = [n for n in started if jw[n] == ju.get(n)]
    d_base = differ(jw, jb)
    good = bool(m) and int(m.group(1)) > 0 and int(on["pending_jobs"]) == int(m.group(1)) \
        and started and len(same_u) == len(started) and d_base
    check("4 hot swap: priorities recomputed, window follows U", bool(good),
          (f"switch at {hm(int(on['sim_time']))} recomputed {m.group(1)} pending jobs in {m.group(2)} us; "
           if m else "no 'priorities recomputed' line for the switch; ")
          + f"{len(started)} jobs started in the window, {len(same_u)} of them exactly as in static U; "
          f"{len(d_base)} of {len(jw)} jobs differ from adaptive BASE")

    # 5. cooldown after each window
    parts, ok = [], not errs
    for _, e, _ in wins:
        off = first_at(rows, e)
        t_end = int(off["sim_time"])
        after = [r for r in rows if t_end <= int(r["sim_time"]) < t_end + G2_TMIN + TICK and r["mode"] == "AUTO"]
        cool = [r for r in after if int(r["sim_time"]) - t_end < G2_TMIN - TICK // 2]
        free = [r for r in after if int(r["sim_time"]) - t_end >= G2_TMIN - TICK // 2]
        good = cool and free and all(r["eligible"] == "0" and r["sampled"] == "0" for r in cool) \
            and free[0]["eligible"] == "1"
        parts.append(f"after {hm(e)}: {len(cool)} ticks not eligible, eligible again at "
                     f"{hm(int(free[0]['sim_time'])) if free else '?'}")
        ok &= bool(good)
    sampled_ineligible = sum(r["sampled"] == "1" and r["eligible"] == "0" for r in rows)
    check("5 cooldown: window ends start a fresh TMin", ok and not sampled_ineligible,
          "; ".join(parts) + f"; {sampled_ineligible} ticks sampled while not eligible")

    # 6. FIXED and FORCED in the plugin
    text_f, rows_f = decisions("ct_fixed_u")
    errs_f = v.validate_decisions(text_f or "", NAMES, None, TICK)
    fixed_ok = rows_f and not errs_f and all(r["mode"] == "FIXED" and r["chosen_policy"] == "U"
                                             and r["switched"] == "0" and r["sampled"] == "0" for r in rows_f) \
        and re.search(r"adaptive: loaded policy=U mode=FIXED", ctld_log("ct_fixed_u"))
    text_x, rows_x = decisions("ct_forced")
    errs_x = v.validate_decisions(text_x or "", NAMES, None, TICK)
    sw = [r for r in rows_x if r["switched"] == "1"]
    alternates = all(a["chosen_policy"] != b["chosen_policy"] for a, b in zip(rows_x, rows_x[1:]))
    recomputed = len(re.findall(r"\(FORCED\): ordering weights changed, priorities recomputed", ctld_log("ct_forced")))
    forced_ok = rows_x and not errs_x and all(r["mode"] == "FORCED" and r["sampled"] == "0" for r in rows_x) \
        and rows_x[0]["chosen_policy"] == "F" and rows_x[0]["switched"] == "0" and alternates \
        and len(sw) == len(rows_x) - 1 and recomputed == len(sw) and all(r["swap_us"] != "" for r in sw)
    check("6 FIXED and FORCED in the plugin", bool(fixed_ok and forced_ok),
          f"FixedPolicy=U: {len(rows_f)} FIXED ticks, "
          f"{sum(r['switched'] == '1' for r in rows_f)} switches{'' if not errs_f else ', INVALID: ' + errs_f[0]}; "
          f"ForceAlternate=F,U every tick: {len(rows_x)} FORCED ticks, {len(sw)} switches, "
          f"{recomputed} recomputes, {'alternating' if alternates else 'NOT alternating'}"
          f"{'' if not errs_x else ', INVALID: ' + errs_x[0]}")

    # INFO: swap cost and the controller's real cost per tick
    decay_cost = float(re.search(r"^DecayCost\s*=\s*([\d.]+)", (VER02 / "config" / "project" / "sim.conf.in")
                                 .read_text(), re.M).group(1)) * 1e6
    for run in ("ct_forced", "pr_forced", "s0212_forced"):
        _, rx = decisions(run)
        pts = [(int(r["pending_jobs"]), int(r["swap_us"])) for r in rx if r["switched"] == "1"]
        if not pts:
            continue
        us = sorted(p[1] for p in pts)
        pend = [p[0] for p in pts]
        busy = [p for p in pts if p[0] > 0]
        slope = icpt = None
        if len(busy) > 2 and len({p[0] for p in busy}) > 1:
            slope, icpt = statistics.linear_regression([p[0] for p in busy], [p[1] for p in busy])
        q = statistics.quantiles(us, n=20) if len(us) > 1 else [us[0]] * 19
        check(f"swap cost {run}", True,
              f"{len(pts)} switches; swap_us median {statistics.median(us):.0f}, p95 {q[18]:.0f}, max {us[-1]}; "
              f"pending jobs median {statistics.median(pend):.0f}, max {max(pend)}"
              + (f"; fit swap_us = {icpt:.1f} + {slope:.3f} x pending" if slope is not None else ""),
              gating=False)
    parts = []
    for run in ("pr_stock", "pr_adaptive", "pr_forced", "s0212_adaptive", "s0212_forced"):
        d = cost(run, "decay")
        if d:
            parts.append(f"{run} {statistics.fmean(d):.0f} (median {statistics.median(d):.0f}, n {len(d)})")
    if parts:
        check("decay event real_usec per tick (controller included)", True,
              "; ".join(parts) + f"; charged DecayCost {decay_cost:.0f}", gating=False)

    print(f"A2 gate: {'PASS' if all(results) else 'FAIL'} ({sum(results)} of {len(results)} gating checks)")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if report() else 1)
