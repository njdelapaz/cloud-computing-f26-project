"""Unit tests for the plugin's config parser (plugin/priority_adaptive/adaptive_conf.c).

Builds adaptive_conf_cli.c with the parser, runs it on good and broken adaptive.conf /
policies.conf files, and checks that each broken file is rejected with the expected message.
Every broken file is also run through scripts/validate.py, so the C parser and the Python
validator are known to agree (two cases are marked C-only: validate.py doesn't open files and
accepts nan).

Run: python3 -I plugin/tests/test_adaptive_conf.py   (needs a C compiler; scripts/check_a1.sh
runs it inside the simulator image)
"""
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
POLICIES = (VER02 / "config" / "policies.conf").read_text()
ADAPTIVE = "\n".join(
    "PoliciesFile=policies.conf" if line.startswith("PoliciesFile=") else line
    for line in (VER02 / "config" / "adaptive.conf").read_text().splitlines()) + "\n"
NAMES = set(v.parse_policies(POLICIES)[0])


def edit(text, startswith, new):
    """Replace the line starting with `startswith` (None as `new` drops it)."""
    lines = text.splitlines()
    out = [new if line.startswith(startswith) else line for line in lines]
    assert out != lines, f"no line starts with {startswith!r}"
    return "\n".join(x for x in out if x is not None) + "\n"


def add(text, line):
    return text + line + "\n"


class Parser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="adaptive_conf_"))
        cls.cli = cls.tmp / "adaptive_conf_cli"
        cc = os.environ.get("CC", "cc")
        subprocess.run([cc, "-std=gnu99", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-Wdeclaration-after-statement", "-I", str(SRC), "-o", str(cls.cli),
                        str(VER02 / "plugin" / "tests" / "adaptive_conf_cli.c"),
                        str(SRC / "adaptive_conf.c"), "-lm"], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_cli(self, adaptive=ADAPTIVE, policies=POLICIES, calc=60, name="case"):
        d = Path(tempfile.mkdtemp(dir=self.tmp, prefix=name))
        (d / "adaptive.conf").write_text(adaptive)
        if policies is not None:
            (d / "policies.conf").write_text(policies)
        p = subprocess.run([str(self.cli), str(d / "adaptive.conf"), str(calc)],
                           capture_output=True, text=True)
        return p.returncode, p.stdout.strip()

    def ok(self, want, **kw):
        rc, out = self.run_cli(**kw)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("OK " + want), out)

    def bad_policies(self, text, fragment, c_only=False):
        rc, out = self.run_cli(policies=text)
        self.assertEqual(rc, 1, out)
        self.assertIn(fragment, out)
        if not c_only:
            self.assertTrue(v.validate_policies(text), "validate.py accepted it")

    def bad_adaptive(self, text, fragment, calc=60, c_only=False):
        rc, out = self.run_cli(adaptive=text, calc=calc)
        self.assertEqual(rc, 1, out)
        self.assertIn(fragment, out)
        if not c_only:
            self.assertTrue(v.validate_adaptive(text, NAMES, calc), "validate.py accepted it")

    # ------------------------------------------------------------ files that load
    def test_repo_config_loads_base(self):
        self.ok("policy=BASE mode=DEFAULT Fairshare=70000 Age=20000 JobSize=10000 policies=11")

    def test_fixed_policy_wins_over_default(self):
        self.ok("policy=U mode=FIXED Fairshare=0 Age=100000 JobSize=0",
                adaptive=add(edit(ADAPTIVE, "DefaultPolicy=", "DefaultPolicy=F"), "FixedPolicy=U"))

    def test_every_policy_can_be_fixed(self):
        want = v.parse_policies(POLICIES)[0]
        for name, p in want.items():
            with self.subTest(policy=name):
                self.ok(f"policy={name} mode=FIXED Fairshare={p['Fairshare']} Age={p['Age']} "
                        f"JobSize={p['JobSize']}", adaptive=add(ADAPTIVE, f"FixedPolicy={name}"))

    def test_default_policy_other_than_base(self):
        self.ok("policy=F>B>U mode=DEFAULT Fairshare=54000 Age=46000",
                adaptive=edit(ADAPTIVE, "DefaultPolicy=", "DefaultPolicy=F>B>U"))

    def test_force_alternate_parses(self):
        self.ok("policy=BASE mode=DEFAULT",
                adaptive=add(add(ADAPTIVE, "ForceAlternate=F,U"), "ForcePeriod=15min"))

    def test_absolute_policies_path(self):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        (d / "p.conf").write_text(POLICIES)
        self.ok("policy=BASE", adaptive=edit(ADAPTIVE, "PoliciesFile=", f"PoliciesFile={d}/p.conf"),
                policies=None)

    def test_relative_path_is_next_to_adaptive_conf(self):
        rc, out = self.run_cli(adaptive=edit(ADAPTIVE, "PoliciesFile=", "PoliciesFile=nope/policies.conf"))
        self.assertEqual(rc, 1)
        self.assertIn("/nope/policies.conf", out)

    def test_comments_blank_lines_tabs_and_crlf(self):
        pol = "# header\n\n" + POLICIES.replace("  ", "\t").replace("\n", "  # trailing\r\n")
        self.ok("policy=BASE", policies=pol)

    def test_tmin_multiple_of_calc_period(self):
        self.ok("policy=BASE", adaptive=edit(ADAPTIVE, "TMin=", "TMin=90s"), calc=30)

    # ------------------------------------------------------------ broken policies.conf
    def test_weights_must_sum(self):
        self.bad_policies(POLICIES.replace("Fairshare=90000  Age=10000", "Fairshare=90000  Age=10001"),
                          "Fairshare+Age+JobSize = 100001")

    def test_jobsize_only_in_base(self):
        self.bad_policies(POLICIES.replace("Fairshare=90000  Age=10000  JobSize=0",
                                           "Fairshare=90000  Age=0      JobSize=10000"),
                          "JobSize must be 0")

    def test_placement_sum(self):
        self.bad_policies(POLICIES.replace("Spread=1.000000 Fit=0.000000", "Spread=0.5 Fit=0.000000"),
                          "Spread+Fit = 0.5")

    def test_base_stock_placement(self):
        self.bad_policies(edit(POLICIES, "PolicyName=BASE ",
                               "PolicyName=BASE Fairshare=70000 Age=20000 JobSize=10000 Spread=1 Fit=0"),
                          "BASE must use stock placement")

    def test_duplicate_policy(self):
        self.bad_policies(add(POLICIES, "PolicyName=F Fairshare=90000 Age=10000 JobSize=0 Spread=0 Fit=0"),
                          "policy 'F' defined twice")

    def test_missing_policy(self):
        self.bad_policies(edit(POLICIES, "PolicyName=EQ ", None), "missing policies: EQ")

    def test_bad_name(self):
        self.bad_policies(add(POLICIES, "PolicyName=F/B Fairshare=90000 Age=10000 JobSize=0 Spread=0 Fit=0"),
                          "bad policy name 'F/B'")

    def test_unknown_key(self):
        self.bad_policies(POLICIES.replace("Fit=0.500000", "Fit=0.500000 Foo=1"), "unknown key 'Foo'")

    def test_missing_key(self):
        self.bad_policies(POLICIES.replace(" Fit=0.500000", ""), "EQ: missing Fit=")

    def test_no_policy_name(self):
        self.bad_policies(add(POLICIES, "Fairshare=90000 Age=10000 JobSize=0 Spread=0 Fit=0"),
                          "must start with PolicyName=")

    def test_not_an_integer(self):
        self.bad_policies(POLICIES.replace("Fairshare=90000", "Fairshare=90k"), "whole numbers >= 0")

    def test_negative_weight(self):
        self.bad_policies(POLICIES.replace("Fairshare=0      Age=100000 JobSize=0      Spread=1",
                                           "Fairshare=-5     Age=100005 JobSize=0      Spread=1"),
                          "whole numbers >= 0")

    def test_spread_out_of_range(self):
        self.bad_policies(POLICIES.replace("Spread=1.000000 Fit=0.000000", "Spread=1.5 Fit=-0.5"),
                          "numbers in [0, 1]")

    def test_token_without_equals(self):
        self.bad_policies(POLICIES.replace("Fit=0.500000", "Fit=0.500000 junk"), "expected Key=Value, got 'junk'")

    def test_key_twice_on_a_line(self):
        self.bad_policies(POLICIES.replace("Fit=0.500000", "Fit=0.500000 Fit=0.5"), "key 'Fit' given twice")

    def test_nan_rejected(self):
        self.bad_policies(POLICIES.replace("Spread=0.500000 Fit=0.500000", "Spread=nan Fit=0.500000"),
                          "numbers in [0, 1]", c_only=True)  # validate.py lets nan through

    def test_empty_policies(self):
        self.bad_policies("# nothing\n", "missing policies: BASE F B U")

    # ------------------------------------------------------------ broken adaptive.conf
    def test_missing_default(self):
        self.bad_adaptive(edit(ADAPTIVE, "DefaultPolicy=", None), "missing DefaultPolicy")

    def test_unknown_default(self):
        self.bad_adaptive(edit(ADAPTIVE, "DefaultPolicy=", "DefaultPolicy=NOPE"),
                          "DefaultPolicy: unknown policy 'NOPE'")

    def test_unknown_fixed(self):
        self.bad_adaptive(add(ADAPTIVE, "FixedPolicy=X>Y"), "FixedPolicy: unknown policy 'X>Y'")

    def test_tmin_format(self):
        self.bad_adaptive(edit(ADAPTIVE, "TMin=", "TMin=90"), "TMin must look like 90s, 15min or 2h")

    def test_tmin_not_multiple(self):
        self.bad_adaptive(edit(ADAPTIVE, "TMin=", "TMin=90s"), "whole multiple of PriorityCalcPeriod (60 s)")

    def test_exit_below_enter(self):
        self.bad_adaptive(edit(ADAPTIVE, "Signal=util", "Signal=util Enter=0.2 Exit=0.2"),
                          "util: Exit must be below Enter")

    def test_unknown_signal(self):
        self.bad_adaptive(edit(ADAPTIVE, "Signal=util", "Signal=memory Enter=0.2 Exit=0.1"),
                          "unknown signal 'memory'")

    def test_missing_signal(self):
        self.bad_adaptive(edit(ADAPTIVE, "Signal=util", None), "missing Signal=util")

    def test_signal_extra_key(self):
        self.bad_adaptive(edit(ADAPTIVE, "Signal=util", "Signal=util Enter=0.2 Exit=0.1 Foo=1"),
                          "Signal lines need exactly")

    def test_min_demand_range(self):
        self.bad_adaptive(edit(ADAPTIVE, "MinDemand=", "MinDemand=1.5"), "MinDemand must be a number in [0, 1]")

    def test_queue_pressure_negative(self):
        self.bad_adaptive(edit(ADAPTIVE, "QueuePressureMin=", "QueuePressureMin=-1"),
                          "QueuePressureMin must be a number >= 0")

    def test_fixed_and_force(self):
        self.bad_adaptive(add(add(add(ADAPTIVE, "FixedPolicy=U"), "ForceAlternate=F,U"), "ForcePeriod=15min"),
                          "FixedPolicy and ForceAlternate can't both be set")

    def test_force_needs_period(self):
        self.bad_adaptive(add(ADAPTIVE, "ForceAlternate=F,U"), "ForceAlternate needs ForcePeriod")

    def test_period_needs_force(self):
        self.bad_adaptive(add(ADAPTIVE, "ForcePeriod=15min"), "ForcePeriod without ForceAlternate")

    def test_force_same_policy(self):
        self.bad_adaptive(add(add(ADAPTIVE, "ForceAlternate=F,F"), "ForcePeriod=15min"),
                          "ForceAlternate needs two different known policies")

    def test_force_unknown_policy(self):
        self.bad_adaptive(add(add(ADAPTIVE, "ForceAlternate=F,Z"), "ForcePeriod=15min"),
                          "ForceAlternate: unknown policy 'Z'")

    def test_unknown_key_adaptive(self):
        self.bad_adaptive(add(ADAPTIVE, "Tmin=60min"), "unknown key 'Tmin'")

    def test_two_keys_on_a_line(self):
        self.bad_adaptive(edit(ADAPTIVE, "MinDemand=", "MinDemand=0.1 QueuePressureMin=0.5"),
                          "one Key=Value per line")

    def test_key_given_twice(self):
        self.bad_adaptive(add(ADAPTIVE, "DefaultPolicy=F"), "'DefaultPolicy' given twice")

    def test_missing_policies_file(self):
        rc, out = self.run_cli(policies=None)
        self.assertEqual(rc, 1)
        self.assertIn("PoliciesFile: can't open", out)  # C-only: validate.py doesn't open files

    def test_missing_adaptive_conf(self):
        p = subprocess.run([str(self.cli), str(self.tmp / "absent.conf")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("can't open", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
