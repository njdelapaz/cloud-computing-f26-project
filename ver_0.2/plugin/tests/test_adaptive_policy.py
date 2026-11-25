"""Unit test of the shared policy state (plugin/adaptive_policy/, A2).

Builds plugin/tests/adaptive_policy_test.c three ways: a host that links the module and exports
its symbols, as slurmctld does, plus a writer and a reader "plugin" that don't contain the module.
The host loads both with dlopen(RTLD_LAZY), the way Slurm loads plugins, and checks that they
share one state, that a reader falls back to nothing before the first publish and after a clear,
and that concurrent readers never see a torn copy.

Run: python3 -I plugin/tests/test_adaptive_policy.py   (needs a C compiler)
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent.parent
SHARED = VER02 / "plugin" / "adaptive_policy"
TEST = VER02 / "plugin" / "tests" / "adaptive_policy_test.c"
FLAGS = ["-std=gnu99", "-O2", "-Wall", "-Wextra", "-Werror", "-Wdeclaration-after-statement",
         "-I", str(SHARED)]


class SharedState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="adaptive_policy_"))
        cc = os.environ.get("CC", "cc")
        for role in ("WRITER", "READER"):
            subprocess.run([cc, *FLAGS, "-fPIC", "-shared", f"-D{role}", "-o",
                            str(cls.tmp / f"{role.lower()}.so"), str(TEST)], check=True)
        subprocess.run([cc, *FLAGS, "-DHOST", "-rdynamic", "-o", str(cls.tmp / "host"), str(TEST),
                        str(SHARED / "adaptive_policy.c"), "-ldl", "-lpthread"], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_plugins_share_one_state(self):
        p = subprocess.run([str(self.tmp / "host"), str(self.tmp / "writer.so"), str(self.tmp / "reader.so")],
                           capture_output=True, text=True, timeout=120)
        print(p.stdout, end="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("FAIL", p.stdout)
        self.assertEqual(p.stdout.count("\nok ") + p.stdout.startswith("ok "), 9)

    def test_plugins_dont_carry_the_module(self):
        # each plugin must use slurmctld's copy, so the module's functions are undefined in it
        nm = shutil.which("nm")
        if not nm:
            self.skipTest("no nm")
        for so in ("writer.so", "reader.so"):
            out = subprocess.run([nm, "-D", str(self.tmp / so)], capture_output=True, text=True).stdout
            defined = [ln for ln in out.splitlines() if "adaptive_policy_" in ln and " U " not in ln]
            self.assertEqual(defined, [], f"{so} defines {defined}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
