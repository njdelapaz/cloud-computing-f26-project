#!/usr/bin/env python3
"""Validators for the ver_0.2 interfaces (SPEC §13). Standard library only.

Usage:
  validate.py policies   FILE
  validate.py schedule   FILE --policies FILE
  validate.py adaptive   FILE --policies FILE [--calc-period SECONDS]
  validate.py jobs       FILE [--meta FILE]
  validate.py meta       FILE
  validate.py episodes   FILE
  validate.py decisions  FILE --policies FILE [--tmin SECONDS [--calc-period SECONDS]]
  validate.py placements FILE --policies FILE
  validate.py examples        check every config and example file in the repo
Exit code 0 when valid; otherwise each problem is printed and the exit code is 1.

Each validate_* function takes the file's text and returns a list of error strings, so tests can
call them directly.
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent

TOTAL_ORDERING = 100_000
NODE_CORES, NODE_MEM_MB = 32, 128_000
REQUIRED_POLICIES = ["BASE", "F", "B", "U", "F>B>U", "F>U>B", "B>F>U", "B>U>F", "U>F>B", "U>B>F", "EQ"]
SIGNALS = ("fairness", "balance", "util")
NAME_RE = re.compile(r"^[A-Za-z0-9_>-]+$")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


# ---------------------------------------------------------------- helpers
def _lines(text):
    """Yield (line number, stripped line) for lines that aren't blank or comments."""
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if line:
            yield n, line


def _kv(tokens, n, errors):
    out = {}
    for t in tokens:
        if "=" not in t:
            errors.append(f"line {n}: expected key=value, got '{t}'")
            continue
        k, v = t.split("=", 1)
        if k in out:
            errors.append(f"line {n}: key '{k}' given twice")
        out[k] = v
    return out


def _int(v, what, errors, lo=None, hi=None):
    try:
        x = int(v)
    except (TypeError, ValueError):
        errors.append(f"{what}: '{v}' is not an integer")
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        errors.append(f"{what}: {x} outside [{lo}, {hi}]")
    return x


def _float(v, what, errors, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        errors.append(f"{what}: '{v}' is not a number")
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        errors.append(f"{what}: {x} outside [{lo}, {hi}]")
    return x


def duration_seconds(v):
    """'90s', '15min', '2h' -> seconds; None if malformed."""
    m = re.fullmatch(r"(\d+)(s|min|h)", v or "")
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "min": 60, "h": 3600}[m.group(2)]


def _csv(text, header, errors):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        errors.append("file is empty")
        return []
    if rows[0] != header:
        errors.append(f"header must be: {','.join(header)}")
        return []
    out = []
    for n, r in enumerate(rows[1:], 2):
        if len(r) != len(header):
            errors.append(f"line {n}: {len(r)} fields, expected {len(header)}")
            continue
        out.append((n, dict(zip(header, r))))
    return out


# ---------------------------------------------------------------- policies.conf
def parse_policies(text):
    """Return ({name: {key: value}}, errors)."""
    errors, policies = [], {}
    for n, line in _lines(text):
        kv = _kv(line.split(), n, errors)
        name = kv.pop("PolicyName", None)
        if name is None:
            errors.append(f"line {n}: must start with PolicyName=")
            continue
        if not NAME_RE.match(name):
            errors.append(f"line {n}: bad policy name '{name}'")
        if name in policies:
            errors.append(f"line {n}: policy '{name}' defined twice")
        want = {"Fairshare", "Age", "JobSize", "Spread", "Fit"}
        if set(kv) != want:
            errors.append(f"line {n}: keys must be exactly {sorted(want)}, got {sorted(kv)}")
            continue
        w = {k: _int(kv[k], f"line {n} {k}", errors, lo=0) for k in ("Fairshare", "Age", "JobSize")}
        p = {k: _float(kv[k], f"line {n} {k}", errors, lo=0.0, hi=1.0) for k in ("Spread", "Fit")}
        if None not in w.values():
            if sum(w.values()) != TOTAL_ORDERING:
                errors.append(f"line {n}: {name}: Fairshare+Age+JobSize = {sum(w.values())}, must be {TOTAL_ORDERING}")
            if name != "BASE" and w["JobSize"] != 0:
                errors.append(f"line {n}: {name}: JobSize must be 0 (only BASE may use it)")
        if None not in p.values():
            s = p["Spread"] + p["Fit"]
            if not (abs(s - 1.0) <= 1e-6 or (p["Spread"] == 0 and p["Fit"] == 0)):
                errors.append(f"line {n}: {name}: Spread+Fit = {s:g}, must be 1 (or both 0 for stock placement)")
            if name == "BASE" and s != 0:
                errors.append(f"line {n}: BASE must use stock placement (Spread=Fit=0)")
        policies[name] = {**w, **p}
    return policies, errors


def validate_policies(text, require_full_set=True):
    policies, errors = parse_policies(text)
    if require_full_set:
        missing = [p for p in REQUIRED_POLICIES if p not in policies]
        if missing:
            errors.append(f"missing policies: {', '.join(missing)}")
    return errors


def stock_placement(policy):
    return policy.get("Spread") == 0 and policy.get("Fit") == 0


# ---------------------------------------------------------------- schedule.conf
def _ts(v):
    try:
        return int(datetime.strptime(v, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _hhmm(v):
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", v or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def validate_schedule(text, policy_names):
    errors, oneoff, daily = [], [], []
    for n, line in _lines(text):
        tokens = line.split()
        if tokens[0] != "Window":
            errors.append(f"line {n}: must start with 'Window'")
            continue
        kv = _kv(tokens[1:], n, errors)
        pol = kv.get("Policy")
        if pol not in policy_names:
            errors.append(f"line {n}: unknown policy '{pol}'")
        if set(kv) == {"Start", "End", "Policy"}:
            s, e = _ts(kv["Start"]), _ts(kv["End"])
            if s is None or e is None:
                errors.append(f"line {n}: Start/End must be YYYY-MM-DDTHH:MM:SS (UTC)")
            elif e <= s:
                errors.append(f"line {n}: End must be after Start")
            else:
                oneoff.append((n, s, e))
        elif set(kv) == {"Daily", "Policy"}:
            parts = kv["Daily"].split("-")
            s, e = (_hhmm(parts[0]), _hhmm(parts[1])) if len(parts) == 2 else (None, None)
            if s is None or e is None:
                errors.append(f"line {n}: Daily must be HH:MM-HH:MM")
            elif e <= s:
                errors.append(f"line {n}: Daily window must end after it starts (no crossing midnight)")
            else:
                daily.append((n, s, e))
        else:
            errors.append(f"line {n}: use either Start/End/Policy or Daily/Policy")
    # overlaps
    for i, (n1, s1, e1) in enumerate(oneoff):
        for n2, s2, e2 in oneoff[i + 1:]:
            if s1 < e2 and s2 < e1:
                errors.append(f"lines {n1} and {n2}: windows overlap")
    for i, (n1, s1, e1) in enumerate(daily):
        for n2, s2, e2 in daily[i + 1:]:
            if s1 < e2 and s2 < e1:
                errors.append(f"lines {n1} and {n2}: daily windows overlap")
    for n1, s1, e1 in oneoff:
        day = datetime.fromtimestamp(s1, timezone.utc).replace(hour=0, minute=0, second=0)
        while day.timestamp() < e1:
            for n2, ds, de in daily:
                a, b = day.timestamp() + 60 * ds, day.timestamp() + 60 * de
                if s1 < b and a < e1:
                    errors.append(f"lines {n1} and {n2}: one-off window overlaps a daily window")
            day += timedelta(days=1)
    return sorted(set(errors), key=errors.index)


# ---------------------------------------------------------------- adaptive.conf
def validate_adaptive(text, policy_names, calc_period=60):
    errors, keys, signals = [], {}, {}
    single = {"PoliciesFile", "ScheduleFile", "TMin", "DefaultPolicy", "MinDemand", "QueuePressureMin",
              "LogFile", "PlacementLog", "FixedPolicy", "ForceAlternate", "ForcePeriod"}
    for n, line in _lines(text):
        tokens = line.split()
        kv = _kv(tokens, n, errors)
        if "Signal" in kv:
            name = kv["Signal"]
            if set(kv) != {"Signal", "Enter", "Exit"}:
                errors.append(f"line {n}: Signal lines need exactly Signal=, Enter= and Exit=")
                continue
            if name not in SIGNALS:
                errors.append(f"line {n}: unknown signal '{name}'")
            if name in signals:
                errors.append(f"line {n}: signal '{name}' given twice")
            enter = _float(kv["Enter"], f"line {n} Enter", errors, 0.0, 1.0)
            exit_ = _float(kv["Exit"], f"line {n} Exit", errors, 0.0, 1.0)
            if enter is not None and exit_ is not None and not exit_ < enter:
                errors.append(f"line {n}: {name}: Exit must be below Enter")
            signals[name] = (enter, exit_)
            continue
        if len(kv) != 1:
            errors.append(f"line {n}: one key=value per line (except Signal lines)")
            continue
        (k, v), = kv.items()
        if k not in single:
            errors.append(f"line {n}: unknown key '{k}'")
        elif k in keys:
            errors.append(f"line {n}: '{k}' given twice")
        keys[k] = (n, v)

    for k in ("PoliciesFile", "ScheduleFile", "TMin", "DefaultPolicy", "MinDemand", "QueuePressureMin", "LogFile"):
        if k not in keys:
            errors.append(f"missing {k}")
    for s in SIGNALS:
        if s not in signals:
            errors.append(f"missing Signal={s}")

    def period(key):
        n, v = keys[key]
        sec = duration_seconds(v)
        if sec is None or sec <= 0:
            errors.append(f"line {n}: {key} must look like 90s, 15min or 2h")
        elif sec % calc_period:
            errors.append(f"line {n}: {key} ({sec} s) must be a whole multiple of PriorityCalcPeriod ({calc_period} s)")

    if "TMin" in keys:
        period("TMin")
    for k in ("DefaultPolicy", "FixedPolicy"):
        if k in keys and keys[k][1] not in policy_names:
            errors.append(f"line {keys[k][0]}: {k}: unknown policy '{keys[k][1]}'")
    if "MinDemand" in keys:
        _float(keys["MinDemand"][1], f"line {keys['MinDemand'][0]} MinDemand", errors, 0.0, 1.0)
    if "QueuePressureMin" in keys:
        _float(keys["QueuePressureMin"][1], f"line {keys['QueuePressureMin'][0]} QueuePressureMin", errors, 0.0)
    if "ForceAlternate" in keys:
        n, v = keys["ForceAlternate"]
        pair = v.split(",")
        if len(pair) != 2 or pair[0] == pair[1] or any(p not in policy_names for p in pair):
            errors.append(f"line {n}: ForceAlternate needs two different known policies, e.g. F,U")
        if "ForcePeriod" not in keys:
            errors.append(f"line {n}: ForceAlternate needs ForcePeriod")
        else:
            period("ForcePeriod")
    elif "ForcePeriod" in keys:
        errors.append(f"line {keys['ForcePeriod'][0]}: ForcePeriod without ForceAlternate")
    if "FixedPolicy" in keys and "ForceAlternate" in keys:
        errors.append("FixedPolicy and ForceAlternate can't both be set")
    return errors


# ---------------------------------------------------------------- neutral trace (jobs.csv) and meta.json
JOBS_HEADER = ["job_id", "submit", "user", "account", "cores", "mem_mb", "time_limit_s", "runtime_s",
               "src_jobid", "src_partition", "src_state"]
SRC_PARTITIONS = {"htc", "htc_highmem", "hpc"}
SRC_STATES = {"completed", "failed", "cancelled", "timeout", "out_of_memory", "node_fail"}


def validate_jobs(text, meta=None):
    errors = []
    rows = _csv(text, JOBS_HEADER, errors)
    last_submit, users, accounts = None, set(), set()
    for i, (n, r) in enumerate(rows, 1):
        e = lambda k: f"line {n} {k}"
        if _int(r["job_id"], e("job_id"), errors) != i:
            errors.append(f"line {n}: job_id must run 1, 2, 3, ... in file order")
        submit = _int(r["submit"], e("submit"), errors, lo=0)
        if submit is not None:
            if last_submit is not None and submit < last_submit:
                errors.append(f"line {n}: rows must be sorted by submit")
            last_submit = submit
            if meta and not (meta["window_start"] <= submit < meta["window_end"]):
                errors.append(f"line {n}: submit outside the window in meta.json")
        for k in ("user", "account", "src_jobid"):
            if not ID_RE.match(r[k]):
                errors.append(f"line {n}: {k} '{r[k]}' is empty or has odd characters")
        _int(r["cores"], e("cores"), errors, 1, NODE_CORES)
        _int(r["mem_mb"], e("mem_mb"), errors, 1, NODE_MEM_MB)
        limit = _int(r["time_limit_s"], e("time_limit_s"), errors, lo=60)
        runtime = _int(r["runtime_s"], e("runtime_s"), errors, lo=1)
        if limit is not None and limit % 60:
            errors.append(f"line {n}: time_limit_s must be whole minutes (the simulator takes minutes)")
        if limit is not None and runtime is not None and runtime > limit:
            errors.append(f"line {n}: runtime_s ({runtime}) above time_limit_s ({limit}); the converter must cap it")
        if r["src_partition"] not in SRC_PARTITIONS:
            errors.append(f"line {n}: src_partition '{r['src_partition']}' isn't one of the kept partitions")
        if r["src_state"] not in SRC_STATES:
            errors.append(f"line {n}: unknown src_state '{r['src_state']}'")
        users.add(r["user"])
        accounts.add(r["account"])
    if meta:
        for k, got in (("jobs", len(rows)), ("users", len(users)), ("accounts", len(accounts))):
            if meta.get(k) != got:
                errors.append(f"meta.json says {k}={meta.get(k)}, jobs.csv has {got}")
    return errors


META_KEYS = {"window_start": int, "window_end": int, "warmup_end": int, "seed": int,
             "sampling_rate": float, "target_load": float, "jobs": int, "users": int, "accounts": int}


def validate_meta(text):
    errors = []
    try:
        m = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"not valid JSON: {exc}"]
    for k, t in META_KEYS.items():
        if k not in m:
            errors.append(f"missing key '{k}'")
        elif t is int and not (isinstance(m[k], int) and not isinstance(m[k], bool)):
            errors.append(f"'{k}' must be an integer")
        elif t is float and not isinstance(m[k], (int, float)):
            errors.append(f"'{k}' must be a number")
    if errors:
        return errors
    if not m["window_start"] <= m["warmup_end"] < m["window_end"]:
        errors.append("need window_start <= warmup_end < window_end")
    if not 0 < m["sampling_rate"] <= 1:
        errors.append("sampling_rate must be in (0, 1]")
    if not 0 < m["target_load"] <= 1:
        errors.append("target_load must be in (0, 1]")
    for k in ("jobs", "users", "accounts"):
        if m[k] < 0:
            errors.append(f"'{k}' can't be negative")
    return errors


# ---------------------------------------------------------------- episodes.csv
EPISODES_HEADER = ["episode_id", "workload", "signal", "onset", "end", "detail"]


def validate_episodes(text):
    errors, ids = [], set()
    for n, r in _csv(text, EPISODES_HEADER, errors):
        eid = _int(r["episode_id"], f"line {n} episode_id", errors, lo=1)
        if eid in ids:
            errors.append(f"line {n}: episode_id {eid} used twice")
        ids.add(eid)
        if r["workload"] not in {f"W{i}" for i in range(1, 7)}:
            errors.append(f"line {n}: workload must be W1..W6")
        if r["signal"] not in (*SIGNALS, "none"):
            errors.append(f"line {n}: signal must be fairness, balance, util or none")
        onset = _int(r["onset"], f"line {n} onset", errors, lo=0)
        end = _int(r["end"], f"line {n} end", errors, lo=0)
        if onset is not None and end is not None and end <= onset:
            errors.append(f"line {n}: end must be after onset")
    return errors


# ---------------------------------------------------------------- decision log
DECISIONS_HEADER = ["sim_time", "mode", "eligible", "sampled", "s_fairness", "s_balance", "s_util",
                    "queue_pressure", "node_load_sd", "active_policy", "chosen_policy", "switched",
                    "swap_us", "pending_jobs"]
MODES = {"AUTO", "OVERRIDE", "FIXED", "FORCED"}
SAMPLE_COLS = ("s_fairness", "s_balance", "s_util", "queue_pressure", "node_load_sd")


def validate_decisions(text, policy_names, tmin=None, calc_period=60):
    """Structural rules, plus the G4 rule: no sampling unless the controller could switch.
    With tmin (seconds), also checks that 'eligible' follows the cooldown: it starts at each
    switch and at the end of each override window (SPEC §8), and it is counted in whole
    controller ticks of calc_period seconds, so a tick within half a tick of tmin counts (A2)."""
    errors = []
    prev = None
    last_switch = None
    for n, r in _csv(text, DECISIONS_HEADER, errors):
        t = _int(r["sim_time"], f"line {n} sim_time", errors, lo=0)
        mode = r["mode"]
        flags = {k: r[k] for k in ("eligible", "sampled", "switched")}
        if mode not in MODES:
            errors.append(f"line {n}: mode must be one of {sorted(MODES)}")
        if any(v not in ("0", "1") for v in flags.values()):
            errors.append(f"line {n}: eligible, sampled and switched must be 0 or 1")
            prev = (n, r, t)
            continue
        eligible, sampled, switched = (flags[k] == "1" for k in ("eligible", "sampled", "switched"))
        for k in ("active_policy", "chosen_policy"):
            if r[k] not in policy_names:
                errors.append(f"line {n}: {k}: unknown policy '{r[k]}'")
        if prev and t is not None and prev[2] is not None and t < prev[2]:
            errors.append(f"line {n}: sim_time goes backwards")
        if prev and r["active_policy"] != prev[1]["chosen_policy"]:
            errors.append(f"line {n}: active_policy should be the previous row's chosen_policy ({prev[1]['chosen_policy']})")
        if mode != "AUTO" and (eligible or sampled):
            errors.append(f"line {n}: in {mode} mode the controller is never eligible and never samples")
        if sampled and not eligible:
            errors.append(f"line {n}: sampled while not eligible to switch (G4)")
        if sampled:
            for k in SAMPLE_COLS:
                hi = 0.5 if k == "node_load_sd" else (None if k == "queue_pressure" else 1.0)
                _float(r[k], f"line {n} {k}", errors, 0.0, hi)
        elif any(r[k] != "" for k in SAMPLE_COLS):
            errors.append(f"line {n}: signal columns must be empty when nothing was sampled")
        if switched != (r["chosen_policy"] != r["active_policy"]):
            errors.append(f"line {n}: switched must be 1 exactly when chosen_policy differs from active_policy")
        if switched:
            _int(r["swap_us"], f"line {n} swap_us", errors, lo=0)
            prev_mode = prev[1]["mode"] if prev else None
            if mode == "AUTO" and not sampled and prev_mode in (None, "AUTO"):
                errors.append(f"line {n}: AUTO switch without sampling (only allowed when a window just ended)")
        elif r["swap_us"] != "":
            errors.append(f"line {n}: swap_us must be empty when nothing switched")
        _int(r["pending_jobs"], f"line {n} pending_jobs", errors, lo=0)
        window_end = prev is not None and prev[1]["mode"] == "OVERRIDE" and mode != "OVERRIDE"
        if tmin is not None and mode == "AUTO" and t is not None:
            should = not window_end and (last_switch is None or t - last_switch >= tmin - calc_period // 2)
            if eligible != should:
                errors.append(f"line {n}: eligible={int(eligible)} but the cooldown says {int(should)}")
        if (switched or window_end) and t is not None:
            last_switch = t
        prev = (n, r, t)
    return errors


# ---------------------------------------------------------------- placement log
PLACEMENTS_HEADER = ["sim_time", "job_id", "policy", "chosen_node", "score", "candidates", "score_us"]


def validate_placements(text, policies):
    errors, jobs, last_t = [], set(), None
    for n, r in _csv(text, PLACEMENTS_HEADER, errors):
        t = _int(r["sim_time"], f"line {n} sim_time", errors, lo=0)
        if t is not None and last_t is not None and t < last_t:
            errors.append(f"line {n}: sim_time goes backwards")
        last_t = t if t is not None else last_t
        j = _int(r["job_id"], f"line {n} job_id", errors, lo=1)
        if j in jobs:
            errors.append(f"line {n}: job {j} placed twice")
        jobs.add(j)
        pol = policies.get(r["policy"])
        if pol is None:
            errors.append(f"line {n}: unknown policy '{r['policy']}'")
        elif stock_placement(pol):
            if r["score"] != "":
                errors.append(f"line {n}: {r['policy']} uses stock placement, so score must be empty")
        else:
            _float(r["score"], f"line {n} score", errors, 0.0, 1.0)
        if not ID_RE.match(r["chosen_node"]):
            errors.append(f"line {n}: chosen_node is empty or has odd characters")
        _int(r["candidates"], f"line {n} candidates", errors, lo=1)
        _int(r["score_us"], f"line {n} score_us", errors, lo=0)
    return errors


# ---------------------------------------------------------------- command line
def check_examples():
    """Validate every config and example file in the repo; return {file: errors}."""
    cfg, ex = VER02 / "config", VER02 / "examples"
    pol_text = (cfg / "policies.conf").read_text()
    policies, _ = parse_policies(pol_text)
    names = set(policies)
    meta = json.loads((ex / "meta.json").read_text())
    return {
        "config/policies.conf": validate_policies(pol_text),
        "config/schedule.conf": validate_schedule((cfg / "schedule.conf").read_text(), names),
        "config/adaptive.conf": validate_adaptive((cfg / "adaptive.conf").read_text(), names),
        "examples/meta.json": validate_meta((ex / "meta.json").read_text()),
        "examples/jobs.csv": validate_jobs((ex / "jobs.csv").read_text(), meta),
        "examples/episodes.csv": validate_episodes((ex / "episodes.csv").read_text()),
        "examples/decisions.csv": validate_decisions((ex / "decisions.csv").read_text(), names, tmin=300),
        "examples/placements.csv": validate_placements((ex / "placements.csv").read_text(), policies),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["policies", "schedule", "adaptive", "jobs", "meta", "episodes",
                                     "decisions", "placements", "examples"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--policies", default=str(VER02 / "config" / "policies.conf"))
    ap.add_argument("--meta")
    ap.add_argument("--calc-period", type=int, default=60)
    ap.add_argument("--tmin", type=int)
    a = ap.parse_args()

    if a.kind == "examples":
        results = check_examples()
    else:
        if not a.file:
            ap.error(f"{a.kind} needs a FILE")
        text = Path(a.file).read_text()
        policies, _ = parse_policies(Path(a.policies).read_text())
        names = set(policies)
        meta = json.loads(Path(a.meta).read_text()) if a.meta else None
        fn = {
            "policies": lambda: validate_policies(text),
            "schedule": lambda: validate_schedule(text, names),
            "adaptive": lambda: validate_adaptive(text, names, a.calc_period),
            "jobs": lambda: validate_jobs(text, meta),
            "meta": lambda: validate_meta(text),
            "episodes": lambda: validate_episodes(text),
            "decisions": lambda: validate_decisions(text, names, a.tmin, a.calc_period),
            "placements": lambda: validate_placements(text, policies),
        }[a.kind]
        results = {a.file: fn()}

    bad = 0
    for f, errs in results.items():
        print(f"{'OK  ' if not errs else 'FAIL'}  {f}")
        for e in errs:
            print(f"        {e}")
        bad += bool(errs)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
