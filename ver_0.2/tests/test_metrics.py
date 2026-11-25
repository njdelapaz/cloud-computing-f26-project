"""Unit tests for analysis/metrics.py (B2, SPEC §10 and §13.4). Standard library only, so they also
run under the plain python3 that scripts/check_p2.sh uses.

The hand-computed trace is tests/data/b2/: its derivation is in README.md there and the values in
hand.json.
Run: python3 -I -m unittest discover -s tests
"""
import contextlib
import csv
import io
import itertools
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02))
sys.path.insert(0, str(VER02 / "scripts"))
import validate as v  # noqa: E402
from analysis import metrics as M  # noqa: E402

DATA = VER02 / "tests" / "data" / "b2"
RUN, TRACE = DATA / "run", DATA / "trace"
HAND = json.loads((DATA / "hand.json").read_text())
T0 = 1704067200
OVERHEAD = M.DECISION_KEYS + M.PLACEMENT_KEYS + M.CHURN_KEYS + M.COST_KEYS


def close(a, b):
    if isinstance(b, str) or a is None or b is None:
        return a == b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def job(jid, submit, start, end, node="n1", cores=1, mem=1000, user="u1", runtime=None):
    return M.Job(jid, jid, user, "a1", submit, start, end, node, cores, mem,
                 end - start if runtime is None else runtime, "COMPLETED")


class Helpers(unittest.TestCase):
    def test_quantile_interpolates_linearly(self):
        self.assertIsNone(M.quantile([], 0.95))
        self.assertEqual(M.quantile([7], 0.95), 7)
        self.assertEqual(M.quantile([0, 0, 100, 160, 170], 0.95), 168)
        self.assertEqual(M.quantile([3, 1, 2, 4], 0.5), 2.5)

    def test_bounded_slowdown(self):
        self.assertEqual(M.bounded_slowdown(0, 4), 1.0)        # 4/10 floored at 1
        self.assertEqual(M.bounded_slowdown(20, 5), 2.5)       # (20 + 5) / max(5, 10)
        self.assertEqual(M.bounded_slowdown(100, 500), 1.2)

    def test_jain_and_ratio(self):
        self.assertEqual(M.jain([2, 2, 2]), 1.0)
        self.assertEqual(M.jain([1, 3]), 0.8)                  # 16 / (2 * 10)
        self.assertIsNone(M.jain([]))
        self.assertEqual(M.ratio(170, 50), 3.4)
        self.assertEqual(M.ratio(5, 0), math.inf)              # a user who never waited
        self.assertEqual(M.ratio(0, 0), 1.0)                   # nobody waited

    def test_hostlist_and_cluster(self):
        self.assertEqual(M.expand_hostlist("n[01-03],x5,y[7,9-10]"), ["n01", "n02", "n03", "x5", "y7", "y9", "y10"])
        c = M.read_cluster(VER02 / "config" / "project" / "slurm.conf.in")
        self.assertEqual((c.names, set(c.cores), set(c.mem_mb)), (M.PROJECT_NODES, {32}, {128000}))
        self.assertEqual(M.read_cluster(VER02 / "no_such_file"), c)           # the project default
        s = M.read_cluster(VER02 / "config" / "smoke" / "slurm.conf.in")
        self.assertEqual((s.names, s.cores, s.mem_mb), (["n1", "n2", "n3"], [4] * 3, [16000] * 3))

    def test_memory_units_and_times(self):
        self.assertEqual(M.parse_mem_mb("5000M"), 5000)
        self.assertEqual(M.parse_mem_mb("4G"), 4096)
        self.assertEqual(M.utc("2024-01-01T00:01:40"), T0 + 100)  # UTC, whatever the host's zone

    def test_meta_formats(self):
        self.assertEqual(M.load_meta(TRACE / "meta.json"),
                         {"window_start": T0, "warmup_end": T0 + 100, "window_end": T0 + 1100})
        with tempfile.TemporaryDirectory() as d:     # scripts/p1_speed_probe.py's shape: no warm-up
            p = Path(d) / "meta.json"
            p.write_text(json.dumps({"time_start": T0, "days": 1.0, "seed": 1}))
            self.assertEqual(M.load_meta(p), {"window_start": T0, "warmup_end": T0, "window_end": T0 + 86400})
            p.write_text("{}")
            with self.assertRaises(ValueError):
                M.load_meta(p)


class Churn(unittest.TestCase):
    def test_kendall_distance(self):
        self.assertEqual(M.kendall_distance([1, 2, 3, 4], [1, 2, 3, 4]), 0)
        self.assertEqual(M.kendall_distance([1, 2, 3, 4], [4, 3, 2, 1]), 1)
        self.assertEqual(M.kendall_distance([1, 2, 3, 4], [2, 1, 3, 4]), 1 / 6)
        self.assertEqual(M.kendall_distance([1, 2, 9], [2, 1, 8]), 1)          # only jobs in both count
        self.assertIsNone(M.kendall_distance([1], [1]))

    def test_kendall_matches_brute_force(self):
        rng = random.Random(7)
        for n in (2, 3, 10, 57):
            a = list(range(n))
            b = a[:]
            rng.shuffle(b)
            pos = {x: i for i, x in enumerate(b)}
            flips = sum(pos[x] > pos[y] for x, y in itertools.combinations(a, 2))
            self.assertAlmostEqual(M.kendall_distance(a, b), flips / (n * (n - 1) / 2))

    def test_churn_per_switch_and_window(self):
        rows = []
        for t, before, after in ((100, "abcd", "badc"), (200, "xyz", "zyx"), (999, "pq", "qp")):
            for phase, order in (("before", before), ("after", after)):
                rows += [{"sim_time": str(t), "phase": phase, "rank": str(r), "job_id": j}
                         for r, j in enumerate(order, 1)]
        got = M.churn_metrics(rows, 0, 500)                   # the switch at 999 is outside
        self.assertEqual(got["churn_n"], 2)
        self.assertAlmostEqual(got["churn_mean"], (1 / 3 + 1) / 2)
        self.assertEqual(got["churn_max"], 1)


class Windows(unittest.TestCase):
    def test_measured_jobs_and_time_average(self):
        # one 4-core node; the window is [100, 200)
        c = M.Cluster(["n1"], [4], [16000])
        warm = job(1, 0, 0, 150, cores=4)                     # warm-up job: counts for load only
        late = job(2, 200, 200, 300, cores=4)                 # submitted at window_end: not measured
        inside = job(3, 120, 150, 160, cores=2, runtime=10)
        jobs = [warm, late, inside]
        measured = [j for j in jobs if 100 <= j.submit < 200]
        self.assertEqual(measured, [inside])
        self.assertEqual(M.job_metrics(measured)["wait_mean_s"], 30)
        n = M.node_metrics(jobs, c, 100, 200)
        self.assertAlmostEqual(n["util_cores"], (4 * 50 + 2 * 10) / (4 * 100))
        self.assertAlmostEqual(n["bal_load_max"], (50 * 1 + 10 * 0.5) / 100)
        self.assertEqual(n["bal_load_sd"], 0)                 # one node: no spread
        # inside is pending in [120, 150) while 0 cores are free: nothing stranded
        self.assertEqual(n["stranded_core_h"], 0)

    def test_stranded_uses_even_median_and_strict_below(self):
        c = M.Cluster(["n1"], [4], [8000])
        holder = job(1, 0, 0, 100, cores=2, mem=4000)         # leaves 2 cores and 4,000 MB free
        p1 = job(2, 0, 100, 110, cores=1, mem=1000)           # 1,000 MB per core
        p2 = job(3, 0, 100, 110, cores=1, mem=3000)           # 3,000 MB per core
        n = M.node_metrics([holder, p1, p2], c, 0, 100)
        self.assertEqual(n["stranded_core_h"], 0)             # 2,000 per core is not below the median 2,000
        p2 = p2._replace(mem_mb=3002)                         # median 2,001: now the 2 free cores are stranded
        n = M.node_metrics([holder, p1, p2], c, 0, 100)
        self.assertAlmostEqual(n["stranded_core_h"], 2 * 100 / 3600)

    def test_log_rows_filtered_by_window(self):
        rows = [{"sim_time": str(t), "switched": "1", "swap_us": str(t)} for t in (99, 100, 150, 200)]
        got, times = M.decision_metrics(rows, 100, 200)
        self.assertEqual((got["switches"], got["swap_us_max"], times), (2, 150, [100, 150]))

    def test_multi_node_job_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "jobcomp.log"
            p.write_text((RUN / "log" / "jobcomp.log").read_text().replace("NodeCnt=1", "NodeCnt=2", 1))
            with self.assertRaises(ValueError):
                M.read_jobcomp(p)


class TinyTrace(unittest.TestCase):
    """The hand-computed trace in tests/data/b2 (README.md there has the calculation)."""

    def test_fixture_is_valid(self):
        policies, _ = v.parse_policies((VER02 / "config" / "policies.conf").read_text())
        meta = json.loads((TRACE / "meta.json").read_text())
        self.assertEqual(v.validate_meta((TRACE / "meta.json").read_text()), [])
        self.assertEqual(v.validate_jobs((TRACE / "jobs.csv").read_text(), meta), [])
        self.assertEqual(v.validate_decisions((RUN / "log" / "decisions.csv").read_text(), set(policies)), [])
        self.assertEqual(v.validate_placements((RUN / "log" / "placements.csv").read_text(), policies), [])

    def test_every_metric_matches_hand_calculation(self):
        row = M.run_metrics(RUN, TRACE / "meta.json", label="b2_tiny")
        self.assertEqual(list(row), M.KEYS)
        self.assertEqual(set(row), set(HAND["tiny"]))
        for k, want in HAND["tiny"].items():
            with self.subTest(metric=k):
                self.assertTrue(close(row[k], want), f"{k}: got {row[k]}, hand {want}")

    def test_examples_logs(self):
        dec, _ = M.decision_metrics(M.read_csv(VER02 / "examples" / "decisions.csv"))
        pl = M.placement_metrics(M.read_csv(VER02 / "examples" / "placements.csv"))
        for got, want in ((dec, HAND["examples_decisions"]), (pl, HAND["examples_placements"])):
            for k, w in want.items():
                with self.subTest(metric=k):
                    self.assertTrue(close(got[k], w), f"{k}: got {got[k]}, hand {w}")

    def test_absent_logs_give_empty_values(self):
        full = M.run_metrics(RUN, TRACE / "meta.json")
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "run"
            shutil.copytree(RUN / "etc", run / "etc")
            (run / "log").mkdir()
            shutil.copy(RUN / "log" / "jobcomp.log", run / "log")
            shutil.copy(TRACE / "meta.json", d)               # no jobs.csv next to it
            row = M.run_metrics(run, Path(d) / "meta.json")
        for k in OVERHEAD:
            self.assertIsNone(row[k], k)
        self.assertEqual((row["runtime_source"], row["jobs_missing"], row["trace_mismatches"]), ("jobcomp", None, None))
        # runtimes from the completion records equal jobs.csv's here, so nothing else changes
        for k in M.JOB_KEYS + M.NODE_KEYS:
            self.assertTrue(close(row[k], full[k]), k)

    def test_command_line(self):
        out = subprocess.run([sys.executable, "-I", str(VER02 / "analysis" / "metrics.py"), str(RUN),
                              "--meta", str(TRACE / "meta.json"), "--label", "b2_tiny"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), M.run_metrics(RUN, TRACE / "meta.json", label="b2_tiny"))

    def test_csv_rows(self):
        row = M.run_metrics(RUN, TRACE / "meta.json", label="b2_tiny")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rows.csv"
            M.append_csv(p, row)
            M.append_csv(p, {**row, "run": "again"})
            with p.open(newline="") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([r["run"] for r in rows], ["b2_tiny", "again"])
        self.assertEqual(float(rows[0]["bal_load_sd"]), row["bal_load_sd"])


class Aggregate(unittest.TestCase):
    def test_ci_over_five_seeds(self):
        rows = [{"run": f"s{i}", "wait_mean_s": float(i), "switches": None} for i in range(1, 6)]
        agg = M.aggregate(rows)
        self.assertNotIn("run", agg)
        self.assertNotIn("switches", agg)                     # empty in every row
        self.assertEqual(agg["wait_mean_s"]["mean"], 3)
        self.assertEqual(agg["wait_mean_s"]["n"], 5)
        # s = sqrt(2.5); half-width = 2.776 * s / sqrt(5)
        self.assertAlmostEqual(agg["wait_mean_s"]["ci95"], 2.776 * math.sqrt(2.5) / math.sqrt(5))

    def test_edge_cases(self):
        self.assertEqual(M.ci95([4.0]), (4.0, None))
        self.assertEqual(M.ci95([1.0, math.inf])[1], None)
        self.assertAlmostEqual(M.ci95([1.0, 3.0])[1], 12.706 * math.sqrt(2) / math.sqrt(2))
        agg = M.aggregate([{"x": 1.0}, {"x": None}, {"x": 3.0}])
        self.assertEqual((agg["x"]["mean"], agg["x"]["n"]), (2.0, 2))

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            files = []
            for i in range(5):
                f = Path(d) / f"s{i}.json"
                f.write_text(json.dumps({"run": f"s{i}", "wait_mean_s": float(i + 1)}))
                files.append(str(f))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                M.main(["aggregate", *files, "--json", str(Path(d) / "agg.json")])
            agg = json.loads((Path(d) / "agg.json").read_text())
        self.assertIn("wait_mean_s", out.getvalue())
        self.assertAlmostEqual(agg["wait_mean_s"]["ci95"], 2.776 * math.sqrt(2.5) / math.sqrt(5))


if __name__ == "__main__":
    unittest.main()
