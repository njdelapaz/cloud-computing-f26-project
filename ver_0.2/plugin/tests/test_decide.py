"""Unit-test table for the controller's decide() (plugin/priority_adaptive/adaptive_ctl.c, A2).

Builds adaptive_ctl_cli.c with the controller and the plugin's parser, and replays scripted
ticks through decide() with real adaptive.conf / policies.conf / schedule.conf files. Each test
checks the decision-log rows and how often the sampler was called, and every log it produces
must also pass scripts/validate.py (SPEC §13.3), with its cooldown check on.

The WORKFLOW A2 gate asks for three groups; they are the classes OverrideBoundaries,
CooldownNoSampling and OverrideVsCooldown. The other classes cover the remaining modes and
rules of SPEC §13.1 and the A2 decisions (adaptive_ctl.h).

Run: python3 -I plugin/tests/test_decide.py   (needs a C compiler; scripts/check_a2.sh runs it
inside the simulator image)
"""
import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(VER02 / "scripts"))
import validate as v  # noqa: E402

SRC = VER02 / "plugin" / "priority_adaptive"
SHARED = VER02 / "plugin" / "adaptive_policy"
POLICIES = (VER02 / "config" / "policies.conf").read_text()
NAMES = set(v.parse_policies(POLICIES)[0])
T0 = 1704067200          # 2024-01-01T00:00:00Z, a tick
TICK = 60
TMIN = 300               # 5 min, the default for these tests


def adaptive_conf(tmin="5min", extra=()):
    lines = ["PoliciesFile=policies.conf", "ScheduleFile=schedule.conf", f"TMin={tmin}",
             "DefaultPolicy=BASE", "Signal=fairness Enter=0.40 Exit=0.30",
             "Signal=balance Enter=0.50 Exit=0.35", "Signal=util Enter=0.25 Exit=0.15",
             "MinDemand=0.10", "QueuePressureMin=0.5", "LogFile=decisions.csv", *extra]
    return "\n".join(lines) + "\n"


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="adaptive_ctl_"))
        cls.cli = cls.tmp / "adaptive_ctl_cli"
        cc = os.environ.get("CC", "cc")
        subprocess.run([cc, "-std=gnu99", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-Wdeclaration-after-statement", "-I", str(SRC), "-I", str(SHARED),
                        "-o", str(cls.cli), str(VER02 / "plugin" / "tests" / "adaptive_ctl_cli.c"),
                        str(SRC / "adaptive_ctl.c"), str(SRC / "adaptive_conf.c"), "-lm"], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_ticks(self, ticks, schedule="", tmin="5min", extra=(), stub=False):
        """ticks: list of sim times, or of (sim_time, options) pairs. Returns the rows, as dicts
        with 'calls' (sampler calls) and 'reorder' added. The log must validate."""
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        (d / "adaptive.conf").write_text(adaptive_conf(tmin, extra))
        (d / "policies.conf").write_text(POLICIES)
        (d / "schedule.conf").write_text(schedule)
        lines = [f"{t[0]} {t[1]}" if isinstance(t, tuple) else str(t) for t in ticks]
        (d / "ticks").write_text("\n".join(lines) + "\n")
        p = subprocess.run([str(self.cli), *(["--stub"] if stub else []), str(d / "adaptive.conf"),
                            str(d / "ticks")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.startswith(",".join(v.DECISIONS_HEADER) + "\n"))
        tmin_s = v.duration_seconds(tmin)
        self.assertEqual(v.validate_decisions(p.stdout, NAMES, tmin_s, TICK), [], p.stdout)
        rows = list(csv.DictReader(io.StringIO(p.stdout)))
        meta = [dict(kv.split("=") for kv in line.split()[2:]) for line in p.stderr.splitlines()]
        self.assertEqual(len(rows), len(meta))
        for r, m in zip(rows, meta):
            r["calls"], r["reorder"], r["window"] = int(m["calls"]), int(m["reorder"]), int(m["window"])
        return rows

    def by_time(self, rows):
        return {int(r["sim_time"]) - T0: r for r in rows}

    def assertRow(self, r, mode=None, eligible=None, sampled=None, active=None, chosen=None,
                  switched=None, calls=None):
        got = {"mode": r["mode"], "eligible": int(r["eligible"]), "sampled": int(r["sampled"]),
               "active": r["active_policy"], "chosen": r["chosen_policy"],
               "switched": int(r["switched"]), "calls": r["calls"]}
        want = {k: x for k, x in dict(mode=mode, eligible=eligible, sampled=sampled, active=active,
                                      chosen=chosen, switched=switched, calls=calls).items()
                if x is not None}
        self.assertEqual({k: got[k] for k in want}, want, f"row at +{int(r['sim_time']) - T0} s")


def every_tick(start, end, step=TICK, **opts):
    """Ticks T0+start .. T0+end (inclusive); opts are tick options for all of them."""
    o = " ".join(f"{k}={x}" for k, x in opts.items())
    return [(T0 + t, o) if o else T0 + t for t in range(start, end + 1, step)]


class OverrideBoundaries(Base):
    """A window [t1, t2) is OVERRIDE from the first tick at or after t1 to the last tick before
    t2, and AUTO again from the first tick at or after t2: within one tick at both ends (G2)."""

    def test_one_off_window_on_the_tick_grid(self):
        sched = f"Window Start={iso(T0 + 600)} End={iso(T0 + 1200)} Policy=U\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 1800), sched, tmin="60min"))
        for t, r in rows.items():
            self.assertEqual(r["mode"], "OVERRIDE" if 600 <= t < 1200 else "AUTO", f"+{t} s")
            # no switch before the window, so no cooldown: every tick before it samples; none
            # inside it, and none in the 60-min cooldown after it
            self.assertEqual(r["calls"], 1 if t < 600 else 0, f"+{t} s")
        self.assertRow(rows[540], mode="AUTO", chosen="BASE", switched=0)
        self.assertRow(rows[600], mode="OVERRIDE", active="BASE", chosen="U", switched=1, eligible=0)
        self.assertRow(rows[1140], mode="OVERRIDE", chosen="U", switched=0)
        self.assertRow(rows[1200], mode="AUTO", active="U", chosen="BASE", switched=1, eligible=0, sampled=0)
        self.assertEqual(rows[600]["window"], 0)
        self.assertEqual(rows[1200]["window"], -1)

    def test_window_off_the_tick_grid(self):
        # t1 and t2 fall between ticks: the switches come on the next tick, under one tick late
        sched = f"Window Start={iso(T0 + 630)} End={iso(T0 + 1230)} Policy=F\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 1800), sched))
        self.assertRow(rows[600], mode="AUTO", chosen="BASE")
        self.assertRow(rows[660], mode="OVERRIDE", chosen="F", switched=1)
        self.assertRow(rows[1200], mode="OVERRIDE", chosen="F", switched=0)
        self.assertRow(rows[1260], mode="AUTO", chosen="BASE", switched=1)

    def test_window_end_is_exclusive_and_start_inclusive(self):
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 180)} Policy=U\n"
        rows = self.by_time(self.run_ticks([T0 + 119, T0 + 120, T0 + 179, T0 + 180], sched))
        self.assertEqual([rows[t]["mode"] for t in (119, 120, 179, 180)],
                         ["AUTO", "OVERRIDE", "OVERRIDE", "AUTO"])

    def test_daily_window_every_day(self):
        sched = "Window Daily=00:10-00:20 Policy=F\n"
        ticks = every_tick(0, 1800) + every_tick(86400, 86400 + 1800)
        rows = self.by_time(self.run_ticks(ticks, sched))
        for day in (0, 86400):
            for t in range(day, day + 1801, TICK):
                self.assertEqual(rows[t]["mode"], "OVERRIDE" if day + 600 <= t < day + 1200 else "AUTO",
                                 f"+{t} s")
            self.assertRow(rows[day + 600], chosen="F", switched=1)
            self.assertRow(rows[day + 1200], mode="AUTO", chosen="BASE", switched=1)

    def test_daily_window_to_23_59(self):
        sched = "Window Daily=23:00-23:59 Policy=U\n"
        rows = self.by_time(self.run_ticks(every_tick(82680, 86460), sched))   # 22:58 .. 00:01
        self.assertRow(rows[82740], mode="AUTO")                 # 22:59
        self.assertRow(rows[82800], mode="OVERRIDE", switched=1)  # 23:00
        self.assertRow(rows[86280], mode="OVERRIDE")             # 23:58
        self.assertRow(rows[86340], mode="AUTO", switched=1)     # 23:59
        self.assertRow(rows[86400], mode="AUTO")                 # 00:00 next day

    def test_adjacent_windows_switch_without_auto_between(self):
        sched = "Window Daily=00:10-00:20 Policy=F\nWindow Daily=00:20-00:30 Policy=U\n"
        rows = self.by_time(self.run_ticks(every_tick(540, 1860), sched))
        self.assertRow(rows[600], mode="OVERRIDE", active="BASE", chosen="F", switched=1)
        self.assertRow(rows[1200], mode="OVERRIDE", active="F", chosen="U", switched=1)
        self.assertRow(rows[1800], mode="AUTO", active="U", chosen="BASE", switched=1)
        self.assertEqual([r["mode"] for t, r in rows.items() if 600 <= t < 1800], ["OVERRIDE"] * 20)

    def test_window_with_the_active_policy(self):
        # nothing to switch at either end, but the window still holds off sampling, and its end
        # still starts a fresh cooldown
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 240)} Policy=BASE\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 600), sched))
        self.assertEqual(sum(int(r["switched"]) for r in rows.values()), 0)
        self.assertRow(rows[60], mode="AUTO", eligible=1, calls=1)
        self.assertRow(rows[120], mode="OVERRIDE", calls=0)
        self.assertRow(rows[240], mode="AUTO", eligible=0, calls=0)
        self.assertRow(rows[480], mode="AUTO", eligible=0, calls=0)
        self.assertRow(rows[540], mode="AUTO", eligible=1, calls=1)


class CooldownNoSampling(Base):
    """After a switch the controller waits TMin, and doesn't sample while it waits (G4)."""

    def test_cooldown_skips_sampling(self):
        ticks = [(T0, "want=F")] + every_tick(60, 600, want="U")
        rows = self.by_time(self.run_ticks(ticks))
        self.assertRow(rows[0], mode="AUTO", eligible=1, sampled=1, calls=1, chosen="F", switched=1)
        for t in range(60, 300, TICK):
            self.assertRow(rows[t], mode="AUTO", eligible=0, sampled=0, calls=0, chosen="F", switched=0)
            self.assertEqual(rows[t]["s_fairness"], "")
        self.assertRow(rows[300], eligible=1, sampled=1, calls=1, active="F", chosen="U", switched=1)
        self.assertEqual(rows[300]["s_fairness"], "0.500000")
        for t in range(360, 600, TICK):
            self.assertRow(rows[t], eligible=0, calls=0)
        self.assertRow(rows[600], eligible=1, calls=1)

    def test_first_tick_is_eligible(self):
        rows = self.run_ticks([(T0 + 17, "want=U")])
        self.assertRow(rows[0], mode="AUTO", eligible=1, sampled=1, calls=1, chosen="U", switched=1)

    def test_no_switch_no_cooldown(self):
        # a sample that keeps the policy doesn't start a cooldown: every tick stays eligible
        rows = self.run_ticks(every_tick(0, 600))
        for r in rows:
            self.assertRow(r, mode="AUTO", eligible=1, sampled=1, calls=1, chosen="BASE", switched=0)

    def test_longer_tmin(self):
        ticks = [(T0, "want=F")] + every_tick(60, 3600, want="U")
        rows = self.by_time(self.run_ticks(ticks, tmin="60min"))
        self.assertEqual(sum(r["calls"] for t, r in rows.items() if 0 < t < 3600), 0)
        self.assertRow(rows[3600], eligible=1, calls=1, chosen="U", switched=1)

    def test_tmin_of_one_tick(self):
        ticks = [(T0, "want=F"), (T0 + 60, "want=U"), (T0 + 120, "want=F")]
        rows = self.run_ticks(ticks, tmin="1min")
        self.assertEqual([r["chosen_policy"] for r in rows], ["F", "U", "F"])
        self.assertEqual([r["calls"] for r in rows], [1, 1, 1])

    def test_ticks_counted_whole(self):
        # ticks land up to a second late or early; the cooldown counts whole ticks
        ticks = [(T0 + 1, "want=F"), T0 + 61, T0 + 119, T0 + 181, T0 + 240, (T0 + 300, "want=U")]
        rows = self.run_ticks(ticks)
        self.assertEqual([int(r["eligible"]) for r in rows], [1, 0, 0, 0, 0, 1])
        self.assertRow(rows[5], chosen="U", switched=1)
        # but half a tick short is still inside the cooldown
        rows = self.run_ticks([(T0, "want=F"), (T0 + 269, "want=U"), (T0 + 270, "want=U")])
        self.assertEqual([int(r["eligible"]) for r in rows], [1, 0, 1])

    def test_sampler_failure_means_no_switch(self):
        # an eligible tick whose sampler can't take a snapshot keeps the policy (a switch in
        # AUTO needs a sample, SPEC §13.3)
        rows = self.run_ticks([(T0, "want=F nosample"), (T0 + 60, "want=F")])
        self.assertRow(rows[0], eligible=1, sampled=0, calls=1, chosen="BASE", switched=0)
        self.assertRow(rows[1], eligible=1, sampled=1, calls=1, chosen="F", switched=1)


class OverrideVsCooldown(Base):
    """A window takes over at once even in a cooldown, and its end switches back to
    DefaultPolicy at once and starts a fresh cooldown (SPEC §8, §11 #3, §13.1)."""

    def test_window_start_and_end_ignore_cooldown(self):
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 240)} Policy=U\n"
        ticks = [(T0, "want=F")] + every_tick(60, 900, want="B")
        rows = self.by_time(self.run_ticks(ticks, sched))
        self.assertRow(rows[0], mode="AUTO", chosen="F", switched=1, calls=1)
        self.assertRow(rows[60], mode="AUTO", eligible=0, calls=0)
        # the window starts inside the cooldown that began at +0 and takes over at once
        self.assertRow(rows[120], mode="OVERRIDE", eligible=0, active="F", chosen="U", switched=1, calls=0)
        self.assertRow(rows[180], mode="OVERRIDE", chosen="U", calls=0)
        # its end goes straight back to DefaultPolicy, without sampling
        self.assertRow(rows[240], mode="AUTO", eligible=0, sampled=0, calls=0, active="U", chosen="BASE",
                       switched=1)
        # and a fresh cooldown runs from the window's end, not from the switch at +0
        for t in range(300, 540, TICK):
            self.assertRow(rows[t], mode="AUTO", eligible=0, calls=0, chosen="BASE")
        self.assertRow(rows[540], mode="AUTO", eligible=1, sampled=1, calls=1, chosen="B", switched=1)

    def test_window_longer_than_tmin(self):
        sched = f"Window Start={iso(T0 + 60)} End={iso(T0 + 900)} Policy=U\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 1500, want="F"), sched))
        self.assertRow(rows[0], chosen="F", switched=1)
        self.assertTrue(all(rows[t]["mode"] == "OVERRIDE" and rows[t]["calls"] == 0 for t in range(60, 900, TICK)))
        self.assertRow(rows[900], mode="AUTO", eligible=0, calls=0, chosen="BASE", switched=1)
        self.assertRow(rows[1140], eligible=0, calls=0)
        self.assertRow(rows[1200], eligible=1, calls=1, chosen="F", switched=1)

    def test_window_on_the_first_tick(self):
        sched = f"Window Start={iso(T0 - 3600)} End={iso(T0 + 120)} Policy=F\n"
        rows = self.run_ticks(every_tick(0, 180, want="U"), sched)
        self.assertRow(rows[0], mode="OVERRIDE", active="BASE", chosen="F", switched=1, calls=0)
        self.assertRow(rows[2], mode="AUTO", eligible=0, chosen="BASE", switched=1, calls=0)


class ExperimentModes(Base):
    """FIXED and FORCED never sample; a window still wins in both (A2 decision)."""

    def test_fixed(self):
        rows = self.run_ticks(every_tick(0, 600, want="F"), extra=["FixedPolicy=U"])
        for r in rows:
            self.assertRow(r, mode="FIXED", eligible=0, sampled=0, calls=0, active="U", chosen="U", switched=0)

    def test_fixed_with_window(self):
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 240)} Policy=F\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 360), sched, extra=["FixedPolicy=U"]))
        self.assertRow(rows[60], mode="FIXED", chosen="U")
        self.assertRow(rows[120], mode="OVERRIDE", active="U", chosen="F", switched=1)
        self.assertRow(rows[240], mode="FIXED", active="F", chosen="U", switched=1, calls=0)
        self.assertRow(rows[300], mode="FIXED", chosen="U", switched=0)

    def test_forced_every_tick(self):
        rows = self.run_ticks(every_tick(0, 300), extra=["ForceAlternate=F,U", "ForcePeriod=1min"])
        self.assertEqual([r["chosen_policy"] for r in rows], ["F", "U", "F", "U", "F", "U"])
        self.assertEqual([int(r["switched"]) for r in rows], [0, 1, 1, 1, 1, 1])
        self.assertTrue(all(r["mode"] == "FORCED" and r["calls"] == 0 and r["eligible"] == "0" for r in rows))

    def test_forced_period(self):
        rows = self.by_time(self.run_ticks(every_tick(0, 1200), extra=["ForceAlternate=F,U", "ForcePeriod=5min"]))
        switches = [t for t, r in rows.items() if r["switched"] == "1"]
        self.assertEqual(switches, [300, 600, 900, 1200])
        self.assertEqual([rows[t]["chosen_policy"] for t in switches], ["U", "F", "U", "F"])

    def test_forced_with_window_restarts_at_first_policy(self):
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 240)} Policy=B\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 600), sched,
                                           extra=["ForceAlternate=F,U", "ForcePeriod=2min"]))
        self.assertRow(rows[0], mode="FORCED", chosen="F", switched=0)
        self.assertRow(rows[120], mode="OVERRIDE", active="F", chosen="B", switched=1)
        self.assertRow(rows[240], mode="FORCED", active="B", chosen="F", switched=1)
        self.assertRow(rows[300], mode="FORCED", chosen="F", switched=0)
        self.assertRow(rows[360], mode="FORCED", active="F", chosen="U", switched=1)


class HotSwapFlag(Base):
    """reorder is set exactly when a switch changes the ordering weights (SPEC §8)."""

    def test_reorder_only_when_ordering_weights_differ(self):
        # B and U share Fairshare=0 Age=100000: placement changes, order doesn't
        rows = self.run_ticks(every_tick(0, 120), extra=["ForceAlternate=B,U", "ForcePeriod=1min"])
        self.assertEqual([(r["switched"], r["reorder"]) for r in rows], [("0", 0), ("1", 0), ("1", 0)])
        rows = self.run_ticks([(T0, "want=U"), (T0 + 300, "want=F>B>U")])
        self.assertEqual([r["reorder"] for r in rows], [1, 1])

    def test_no_switch_no_reorder(self):
        rows = self.run_ticks(every_tick(0, 120))
        self.assertEqual([r["reorder"] for r in rows], [0, 0, 0])


class A2Stub(Base):
    """The plugin's own setup in A2: no sampler and no classifier, so AUTO keeps DefaultPolicy."""

    def test_auto_keeps_default(self):
        rows = self.run_ticks(every_tick(0, 600), stub=True)
        for r in rows:
            self.assertRow(r, mode="AUTO", eligible=1, sampled=0, calls=0, chosen="BASE", switched=0)

    def test_stub_with_window(self):
        sched = f"Window Start={iso(T0 + 120)} End={iso(T0 + 300)} Policy=U\n"
        rows = self.by_time(self.run_ticks(every_tick(0, 900), sched, stub=True))
        self.assertRow(rows[60], mode="AUTO", eligible=1, sampled=0, chosen="BASE")
        self.assertRow(rows[120], mode="OVERRIDE", chosen="U", switched=1)
        self.assertRow(rows[300], mode="AUTO", eligible=0, chosen="BASE", switched=1)
        self.assertRow(rows[540], mode="AUTO", eligible=0)
        self.assertRow(rows[600], mode="AUTO", eligible=1, sampled=0, chosen="BASE")


if __name__ == "__main__":
    unittest.main(verbosity=1)
