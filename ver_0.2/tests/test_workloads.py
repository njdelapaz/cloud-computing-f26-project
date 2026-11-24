"""Unit tests for the B1 workload pipeline: filter, sampler, converter and episode finder.

The converter and the shared helpers are standard-library only and always run. The filter,
sampler and episode finder need numpy, pandas and pyarrow; without them (as under the plain
python3 that scripts/check_p2.sh uses) those tests are skipped.
Run: .venv/bin/python -I -m unittest discover -s tests
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

VER02 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VER02))
sys.path.insert(0, str(VER02 / "scripts"))
import validate as v  # noqa: E402
from workloads.convert import common as T  # noqa: E402
from workloads.convert import to_sim  # noqa: E402

try:
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAVE_PD = True
except ImportError:
    HAVE_PD = False
needs_pd = unittest.skipUnless(HAVE_PD, "needs numpy, pandas and pyarrow (use .venv/bin/python)")

FEB = T.parse_time("2024-02-12")       # a Monday inside the trace's full-coverage span


class Common(unittest.TestCase):
    def test_durations(self):
        for s, want in (("1d", 86400), ("6h", 21600), ("15min", 900), ("90s", 90), ("120", 120), ("1.5d", 129600)):
            self.assertEqual(T.parse_duration(s), want)
        with self.assertRaises(ValueError):
            T.parse_duration("1 week")

    def test_time_limit_rounds_up(self):
        self.assertEqual([T.time_limit_minutes(s) for s in (1, 59, 60, 61, 3600, 3601)], [1, 1, 1, 2, 60, 61])

    def test_split_and_times(self):
        self.assertEqual(T.parse_time("2024-03-01"), T.EVAL_START)
        self.assertEqual(T.split_of(T.EVAL_START - 1), "calibration")
        self.assertEqual(T.split_of(T.EVAL_START), "evaluation")
        self.assertEqual(T.iso(T.COVERAGE_END), "2024-06-23T22:00:00Z")
        self.assertEqual(T.CORES, 512)


def write_trace(d, jobs, meta):
    d = Path(d)
    with open(d / "jobs.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(v.JOBS_HEADER)
        w.writerows(jobs)
    (d / "meta.json").write_text(json.dumps(meta))


class Converter(unittest.TestCase):
    T0 = 1705276800

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = Path(self.dir.name)
        jobs = [[1, self.T0, "user0002", "account002", 1, 4000, 60, 60, "001", "htc", "completed"],
                [2, self.T0 + 100, "user0001", "account001", 32, 128000, 172800, 5000, "002", "hpc", "timeout"],
                [3, self.T0 + 100, "user0001", "account003", 4, 64, 1800, 10, "003", "htc", "failed"],
                [4, self.T0 + 200, "user0001", "account001", 2, 1000, 600, 30, "004", "htc", "completed"]]
        meta = {"window_start": self.T0, "window_end": self.T0 + 86400, "warmup_end": self.T0 + 3600,
                "seed": 1, "sampling_rate": 0.5, "target_load": 0.85, "jobs": 4, "users": 2, "accounts": 3}
        write_trace(self.d, jobs, meta)
        self.assertEqual(v.validate_jobs((self.d / "jobs.csv").read_text(), meta), [])
        self.res = to_sim.convert(self.d)

    def tearDown(self):
        self.dir.cleanup()

    def test_event_lines(self):
        lines = (self.d / "jobs.events").read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[1],
                         "-dt 100 -e submit_batch_job | -J jobid_2 -sim-walltime 5000 --uid=user0001 -t 2880 "
                         "-N 1 -n 32 --ntasks-per-node=32 --mem=128000 -A account001 -p normal -q normal pseudo.job")
        self.assertTrue(lines[0].startswith("-dt 0 ") and " -t 1 " in lines[0])

    def test_users_and_accounts(self):
        users = (self.d / "users.sim").read_text().splitlines()
        self.assertEqual(users, ["admin:1000:admin:1000", "user0001:2000:users:100", "user0002:2001:users:100"])
        sa = (self.d / "sacctmgr.script").read_text().splitlines()
        accounts = [line for line in sa if line.startswith("add account")]
        self.assertEqual(len(accounts), 3)
        self.assertTrue(all(line.endswith("Fairshare=100") for line in accounts), accounts)
        self.assertIn("add cluster Name=project Fairshare=1 QOS=normal", sa)
        # user0001 ran two jobs under account001 and one under account003
        self.assertIn("add user name=user0001 DefaultAccount=account001", sa)
        self.assertIn("add user name=user0001 Account=account003", sa)

    def test_time_start(self):
        self.assertEqual((self.d / "sim.env").read_text(), f"TIME_START={self.T0 - 30}\n")
        self.assertEqual(self.res["time_start"], self.T0 - 30)


def source_frame(rows):
    cols = ["jobid", "state", "account", "user", "partition", "submit", "start", "timelimit", "elapsed",
            "allocmem", "alloccpus", "started", "hostnames"]
    return pd.DataFrame(rows, columns=cols)


@needs_pd
class Filter(unittest.TestCase):
    def test_each_rule(self):
        from workloads.convert import filter_trace as F
        t = FEB
        ok = ["completed", "a1", "u1", "htc", t, t, 3600, 100, 4000, 1, 1, "a001"]
        rows = [
            ["01"] + ok,                                                                    # kept
            ["02", "completed", "a1", "u1", "gpu", t, t, 3600, 100, 4000, 1, 1, "g001"],    # partition
            ["03", "cancelled", "a1", "u1", "htc", t, None, 3600, 0, None, 1, 0, "None"],   # never started
            ["04", "cancelled", "a1", "u1", "htc", t, t, 3600, 50, None, 1, 0, "a001"],     # started=0
            ["05", "failed", "a1", "u1", "hpc", t, t, 3600, 0, 4000, 1, 1, "a001"],         # zero elapsed
            ["06", "completed", "a1", "u1", "hpc", t, t, 3600, 100, 4000, 33, 1, "a001"],   # > 32 cores
            ["07", "completed", "a1", "u1", "htc_highmem", t, t, 3600, 100, 128001, 1, 1, "a001"],  # > 128 GB
            ["08", "completed", "a1", "u1", "hpc", t, t, 3600, 100, 4000, 8, 1, "p001,p002"],      # multi-node
            ["09", "completed", "a1", "u1", "hpc", t, t, 3600, 100, 4000, 8, 1, "p[001-002]"],     # multi-node
            ["10", "timeout", "a1", "u1", "htc", t, t, 3600, 3616, 128000, 32, 1, "a002"],  # kept, capped
        ]
        kept, c = F.filter_month(source_frame(rows))
        self.assertEqual(list(kept.jobid), ["01", "10"])
        self.assertEqual((c["source"], c["after_partition"], c["never_started"], c["zero_elapsed"]), (10, 9, 2, 1))
        self.assertEqual((c["too_big"], c["too_big_cores"], c["too_big_mem"], c["too_big_multinode"]), (4, 1, 1, 2))
        self.assertEqual(c["capped"], 1)
        self.assertEqual(list(kept.runtime), [100, 3600])
        self.assertEqual(list(kept.cores), [1, 32])
        self.assertAlmostEqual(c["too_big_core_hours"], (33 + 1 + 8 + 8) * 100 / 3600)


def cache_frame(n_days=4, per_hour=40, seed=0, start=FEB - 86400):
    """A filtered-trace-like frame: per_hour jobs every hour, mixed sizes, with a few large jobs."""
    rng = np.random.default_rng(seed)
    n = n_days * 24 * per_hour
    submit = start + np.sort(rng.integers(0, n_days * 86400, n))
    cores = rng.choice([1, 1, 1, 2, 4, 8, 32], n)
    runtime = rng.integers(60, 6 * 3600, n)
    big = rng.random(n) < 0.002
    runtime[big] = 4 * 86400
    return pd.DataFrame({
        "jobid": [f"{i:08d}" for i in range(n)], "state": "completed",
        "account": [f"account{i % 7:03d}" for i in range(n)], "user": [f"user{i % 13:04d}" for i in range(n)],
        "partition": "htc", "submit": submit.astype("int64"),
        "timelimit": (runtime + rng.integers(0, 7200, n)).astype("int64"), "runtime": runtime.astype("int64"),
        "cores": cores.astype("int64"),
        "mem_mb": np.minimum(cores * rng.choice([1000, 4000, 10000], n), T.NODE_MEM_MB).astype("int64")})


@needs_pd
class Sampler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from workloads.convert import sample as S
        cls.S = S
        cls.df = cache_frame()

    def window(self):
        return self.df[(self.df.submit >= FEB - 86400) & (self.df.submit < FEB + 86400)].reset_index(drop=True)

    def test_hits_target(self):
        w = self.window()
        for seed in range(1, 21):
            s, p, full, skipped = self.S.sample(w, FEB, FEB + 86400, seed, 0.85)
            m = s.submit >= FEB
            load = self.S.offered_load(s.cores[m], s.runtime[m], 86400)
            self.assertGreaterEqual(load, 0.85)
            self.assertLessEqual(load, 0.85 * 1.02)
            self.assertTrue(0 < p <= 1)

    def test_deterministic_and_seeded(self):
        w = self.window()
        a, pa_, _, _ = self.S.sample(w, FEB, FEB + 86400, 3, 0.85)
        b, pb, _, _ = self.S.sample(w, FEB, FEB + 86400, 3, 0.85)
        c, _, _, _ = self.S.sample(w, FEB, FEB + 86400, 4, 0.85)
        self.assertEqual(list(a.jobid), list(b.jobid))
        self.assertEqual(pa_, pb)
        self.assertNotEqual(list(a.jobid), list(c.jobid))

    def test_warmup_uses_same_rate(self):
        w = self.window()
        u = np.random.default_rng(5).random(len(w))
        s, p, _, _ = self.S.sample(w, FEB, FEB + 86400, 5, 0.85)
        warm = set(s.jobid[s.submit < FEB])
        self.assertEqual(warm, set(w.jobid[(w.submit < FEB) & (u <= p)]))

    def test_too_little_demand(self):
        w = self.window()
        with self.assertRaises(SystemExit):
            self.S.sample(w, FEB, FEB + 86400, 1, 1000.0)

    def test_run_writes_valid_files(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache.parquet"
            df = self.df.copy()
            df.loc[0, "timelimit"] = 61              # rounds up to 120 s
            df.loc[0, "runtime"] = 61
            pq.write_table(pa.Table.from_pandas(df, preserve_index=False), cache)
            stats = self.S.run(FEB, 86400, 86400, 1, 0.85, cache, Path(d) / "out")
            meta_text = (Path(d) / "out" / "meta.json").read_text()
            meta = json.loads(meta_text)
            self.assertEqual(v.validate_meta(meta_text), [])
            self.assertEqual(v.validate_jobs((Path(d) / "out" / "jobs.csv").read_text(), meta), [])
            self.assertEqual(sorted(meta), sorted(v.META_KEYS))
            self.assertEqual((meta["window_start"], meta["warmup_end"], meta["window_end"]),
                             (FEB - 86400, FEB, FEB + 86400))
            self.assertAlmostEqual(stats["offered_load"], 0.85, delta=0.85 * 0.02)
            jobs = pd.read_csv(Path(d) / "out" / "jobs.csv")
            self.assertTrue((jobs.time_limit_s % 60 == 0).all())
            self.assertTrue((jobs.runtime_s <= jobs.time_limit_s).all())
            # outside the full-coverage span is refused
            with self.assertRaises(SystemExit):
                self.S.run(T.parse_time("2024-01-01"), 86400, 86400, 1, 0.85, cache, Path(d) / "x")


def episode_frame():
    """Background load every hour, one planted flood, one planted burst right after it, and one
    near-idle bin where memory-heavy jobs make a high share of a small volume."""
    H = 3600
    rows = []
    flood_h = (T.parse_time("2024-02-10T14:00") - T.EPISODE_START) // H
    burst_h = (T.parse_time("2024-02-10T19:00") - T.EPISODE_START) // H
    idle_b = (T.parse_time("2024-04-01T00:00") - T.EPISODE_START) // (6 * H)
    for h in range((T.EPISODE_END - T.EPISODE_START) // H):
        t = T.EPISODE_START + h * H
        idle = h // 6 == idle_b
        if not idle:
            for u in range(4):
                rows.append((t + 60 * u, f"user{u:04d}", 1, H, 2000))      # light, 1 core-hour each
        rows.append((t + 300, "user0005", 1, H // 2 if idle else H, 10000))  # heavy, 20% share
        if h == flood_h:
            rows += [(t + 600 + i, "user0009", 4, H, 2000) for i in range(30)]
        if burst_h <= h < burst_h + 2:       # two users, so neither one floods the bin
            rows += [(t + 900 + i, f"user000{6 + i % 2}", 1, H, 16000) for i in range(20)]
    return pd.DataFrame(rows, columns=["submit", "user", "cores", "runtime", "mem_mb"]), flood_h, burst_h, idle_b


@needs_pd
class Episodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(VER02 / "workloads" / "episodes"))
        import find_episodes as E
        cls.E = E
        df, cls.flood_h, cls.burst_h, cls.idle_b = episode_frame()
        cls.df = E.prepare(df)
        cls.tb = E.Tables(cls.df)

    def t(self, h):
        return T.EPISODE_START + h * 3600

    def test_flood(self):
        w1, nbins = self.E.find_w1(self.tb)
        self.assertEqual(nbins, 1)
        self.assertEqual(len(w1), 1)
        e = w1[0]
        self.assertEqual((e["user"], e["onset"], e["end"]), ("user0009", self.t(self.flood_h), self.t(self.flood_h + 1)))
        self.assertEqual(T.split_of(e["onset"]), "calibration")

    def test_burst_and_volume_floor(self):
        w2, _ = self.E.find_w2(self.tb)
        self.assertEqual([(e["onset"], e["end"]) for e in w2], [(self.t(self.burst_h), self.t(self.burst_h + 2))])
        start_rule, _ = self.E.find_w2(self.tb, min_heavy=0)
        idle_t = T.EPISODE_START + self.idle_b * 6 * 3600
        self.assertIn(idle_t, [e["onset"] for e in start_rule])      # the starting rule takes the idle bin
        self.assertNotIn(idle_t, [e["onset"] for e in w2])           # the floor drops it

    def test_sequence(self):
        w1, _ = self.E.find_w1(self.tb)
        w2, _ = self.E.find_w2(self.tb)
        for i, e in enumerate(w1 + w2, 1):
            e["episode_id"] = i
        w4 = self.E.find_w4(w1 + w2)
        self.assertEqual(len(w4), 1)
        self.assertEqual(w4[0]["detail"], "seq=W1#1>W2#2")
        self.assertEqual(w4[0]["signal"], "fairness")
        self.assertEqual((w4[0]["onset"], w4[0]["end"]), (self.t(self.flood_h), self.t(self.burst_h + 2)))

    def test_daily_peak_and_csv(self):
        w6, prof = self.E.find_w6(self.tb)
        self.assertEqual(len(w6), 182)
        self.assertEqual(w6[0]["onset"] - T.EPISODE_START, 12 * 3600)
        self.assertEqual(w6[0]["end"] - w6[0]["onset"], 5 * 3600)
        w1, _ = self.E.find_w1(self.tb)
        w2, _ = self.E.find_w2(self.tb)
        eps = w1 + w2 + w6
        for i, e in enumerate(eps, 1):
            e["episode_id"] = i
        with tempfile.TemporaryDirectory() as d:
            self.E.write_csv(Path(d) / "episodes.csv", eps)
            text = (Path(d) / "episodes.csv").read_text()
        self.assertEqual(v.validate_episodes(text), [])
        details = [line.split(",", 5)[5] for line in text.splitlines()[1:]]
        self.assertTrue(all(dt.startswith(("split=calibration", "split=evaluation")) for dt in details))
        self.assertTrue(any("coverage=partial" in dt for dt in details))   # late-June peaks

    def test_refine_walks_back(self):
        hits = {10, 11, 12, 15}
        on, off, refined = self.E.refine(2, 2, lambda h: h in hits)      # bin 2 = hours 12..17
        self.assertEqual((on, off, refined), (10, 16, True))
        on, off, refined = self.E.refine(2, 2, lambda h: False)
        self.assertEqual((on, off, refined), (12, 18, False))


if __name__ == "__main__":
    unittest.main()
