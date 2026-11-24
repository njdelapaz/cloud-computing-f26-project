"""Unit tests for scripts/validate.py: every example passes, and each rule rejects a broken file.
Run: python3 -m unittest discover -s tests
"""
import json
import sys
import unittest
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02 / "scripts"))
import validate as v  # noqa: E402

CFG, EX = VER02 / "config", VER02 / "examples"
POLICIES_TEXT = (CFG / "policies.conf").read_text()
POLICIES, _ = v.parse_policies(POLICIES_TEXT)
NAMES = set(POLICIES)


def replace_line(text, startswith, new):
    out = [new if line.startswith(startswith) else line for line in text.splitlines()]
    assert out != text.splitlines(), f"no line starts with {startswith!r}"
    return "\n".join(out) + "\n"


class Examples(unittest.TestCase):
    def test_all_examples_valid(self):
        for f, errs in v.check_examples().items():
            with self.subTest(file=f):
                self.assertEqual(errs, [])

    def test_policies_file_is_generated(self):
        sys.path.insert(0, str(VER02 / "scripts"))
        import make_policies
        self.assertEqual(make_policies.render(), POLICIES_TEXT)


class Policies(unittest.TestCase):
    def bad(self, text, fragment):
        errs = v.validate_policies(text)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_ordering_sum(self):
        self.bad(replace_line(POLICIES_TEXT, "PolicyName=F ",
                              "PolicyName=F Fairshare=90000 Age=9000 JobSize=0 Spread=0 Fit=0"), "must be 100000")

    def test_jobsize_only_in_base(self):
        self.bad(replace_line(POLICIES_TEXT, "PolicyName=F ",
                              "PolicyName=F Fairshare=80000 Age=10000 JobSize=10000 Spread=0 Fit=0"), "JobSize must be 0")

    def test_placement_sum(self):
        self.bad(replace_line(POLICIES_TEXT, "PolicyName=B ",
                              "PolicyName=B Fairshare=0 Age=100000 JobSize=0 Spread=0.7 Fit=0.2"), "Spread+Fit")

    def test_base_stock_placement(self):
        self.bad(replace_line(POLICIES_TEXT, "PolicyName=BASE",
                              "PolicyName=BASE Fairshare=70000 Age=20000 JobSize=10000 Spread=1 Fit=0"), "stock placement")

    def test_duplicate_and_missing(self):
        text = replace_line(POLICIES_TEXT, "PolicyName=EQ",
                            "PolicyName=F Fairshare=90000 Age=10000 JobSize=0 Spread=0 Fit=0")
        errs = v.validate_policies(text)
        self.assertTrue(any("defined twice" in e for e in errs), errs)
        self.assertTrue(any("missing policies: EQ" in e for e in errs), errs)


class Schedule(unittest.TestCase):
    def errs(self, text):
        return v.validate_schedule(text, NAMES)

    def test_ok(self):
        self.assertEqual(self.errs("Window Daily=12:00-17:00 Policy=U\n"), [])

    def test_unknown_policy(self):
        self.assertTrue(any("unknown policy" in e for e in self.errs("Window Daily=12:00-17:00 Policy=X\n")))

    def test_end_before_start(self):
        text = "Window Start=2024-04-08T09:00:00 End=2024-04-08T06:00:00 Policy=F\n"
        self.assertTrue(any("End must be after Start" in e for e in self.errs(text)))

    def test_oneoff_overlaps_daily(self):
        text = ("Window Daily=12:00-17:00 Policy=U\n"
                "Window Start=2024-04-08T16:00:00 End=2024-04-08T18:00:00 Policy=F\n")
        self.assertTrue(any("overlaps a daily window" in e for e in self.errs(text)))

    def test_daily_overlap(self):
        text = "Window Daily=12:00-17:00 Policy=U\nWindow Daily=16:00-18:00 Policy=F\n"
        self.assertTrue(any("daily windows overlap" in e for e in self.errs(text)))


class Adaptive(unittest.TestCase):
    TEXT = (CFG / "adaptive.conf").read_text()

    def errs(self, text, period=60):
        return v.validate_adaptive(text, NAMES, period)

    def test_exit_below_enter(self):
        text = replace_line(self.TEXT, "Signal=util", "Signal=util Enter=0.20 Exit=0.25")
        self.assertTrue(any("Exit must be below Enter" in e for e in self.errs(text)))

    def test_missing_signal(self):
        text = replace_line(self.TEXT, "Signal=balance", "")
        self.assertTrue(any("missing Signal=balance" in e for e in self.errs(text)))

    def test_tmin_multiple_of_calc_period(self):
        self.assertTrue(any("whole multiple" in e for e in self.errs(self.TEXT, period=7 * 60)))

    def test_fixed_and_forced_exclusive(self):
        text = self.TEXT + "FixedPolicy=F\nForceAlternate=F,U\nForcePeriod=15min\n"
        self.assertTrue(any("can't both be set" in e for e in self.errs(text)))

    def test_force_needs_period(self):
        self.assertTrue(any("needs ForcePeriod" in e for e in self.errs(self.TEXT + "ForceAlternate=F,U\n")))


class Jobs(unittest.TestCase):
    TEXT = (EX / "jobs.csv").read_text()
    META = json.loads((EX / "meta.json").read_text())

    def edit_row(self, row, col, value):
        lines = self.TEXT.splitlines()
        header = lines[0].split(",")
        fields = lines[row].split(",")
        fields[header.index(col)] = value
        lines[row] = ",".join(fields)
        return "\n".join(lines) + "\n"

    def bad(self, text, fragment):
        errs = v.validate_jobs(text)
        self.assertTrue(any(fragment in e for e in errs), errs)

    def test_runtime_above_limit(self):
        self.bad(self.edit_row(1, "runtime_s", "999999"), "must cap it")

    def test_too_many_cores(self):
        self.bad(self.edit_row(1, "cores", "33"), "outside")

    def test_limit_whole_minutes(self):
        self.bad(self.edit_row(1, "time_limit_s", "165601"), "whole minutes")

    def test_unsorted(self):
        self.bad(self.edit_row(2, "submit", "1705276000"), "sorted by submit")

    def test_meta_counts(self):
        meta = dict(self.META, jobs=9)
        self.assertTrue(any("jobs=9" in e for e in v.validate_jobs(self.TEXT, meta)))

    def test_meta_window_order(self):
        bad = dict(self.META, warmup_end=self.META["window_end"])
        self.assertTrue(v.validate_meta(json.dumps(bad)))


class Decisions(unittest.TestCase):
    TEXT = (EX / "decisions.csv").read_text()

    def errs(self, text, tmin=300):
        return v.validate_decisions(text, NAMES, tmin)

    def row(self, i, new):
        lines = self.TEXT.splitlines()
        lines[i] = new
        return "\n".join(lines) + "\n"

    def test_sampling_during_cooldown(self):
        # row 3 is inside the cooldown after the switch at row 2
        text = self.row(3, "1712555520,AUTO,0,1,0.40,0.20,0.05,1.0,0.10,F,F,0,,63")
        self.assertTrue(any("G4" in e for e in self.errs(text)))

    def test_eligible_follows_cooldown(self):
        text = self.row(3, "1712555520,AUTO,1,0,,,,,,F,F,0,,63")
        self.assertTrue(any("cooldown says 0" in e for e in self.errs(text)))

    def test_switch_flag_consistency(self):
        text = self.row(1, "1712555400,AUTO,1,1,0.12,0.20,0.05,0.80,0.10,BASE,BASE,1,500,41")
        self.assertTrue(any("switched must be 1 exactly" in e for e in self.errs(text)))

    def test_continuity(self):
        text = self.row(3, "1712555520,AUTO,0,0,,,,,,BASE,BASE,0,,63")
        self.assertTrue(any("previous row's chosen_policy" in e for e in self.errs(text)))

    def test_override_never_samples(self):
        text = self.row(12, "1712556060,OVERRIDE,0,1,0.1,0.1,0.1,0.5,0.05,F,F,0,,36")
        self.assertTrue(any("never eligible and never samples" in e for e in self.errs(text)))


class Placements(unittest.TestCase):
    TEXT = (EX / "placements.csv").read_text()

    def errs(self, text):
        return v.validate_placements(text, POLICIES)

    def test_score_empty_for_stock(self):
        text = self.TEXT.replace("1712555410,101,BASE,n03,,16,0", "1712555410,101,BASE,n03,0.5,16,0")
        self.assertTrue(any("score must be empty" in e for e in self.errs(text)))

    def test_job_placed_twice(self):
        text = self.TEXT + "1712556400,107,B,n12,0.700,16,11\n"
        self.assertTrue(any("placed twice" in e for e in self.errs(text)))


if __name__ == "__main__":
    unittest.main()
