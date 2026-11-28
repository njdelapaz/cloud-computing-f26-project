"""Unit tests for the simulator run helpers (no Docker needed)."""
from pathlib import Path

import pytest

from sim.check_run import mem_mb, parse_events, slurm_seconds
from sim.prepare_run import prepare_run, read_accounts, sacctmgr_script, users_sim

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [("acct1", "u1"), ("acct2", "u2"), ("acct3", "u2")]


def test_users_sim_unique_ids():
    lines = users_sim(PAIRS).splitlines()
    names = [l.split(":")[0] for l in lines]
    uids = [l.split(":")[1] for l in lines]
    assert names == ["slurm", "u1", "u2"]
    assert len(set(uids)) == len(uids)


def test_sacctmgr_creates_size_qos_with_priorities():
    s = sacctmgr_script(PAIRS, {"small": 0, "medium": 50, "large": 100})
    assert "add qos name=small Priority=0" in s and "add qos name=large Priority=100" in s
    assert "QOS=normal,small,medium,large" in s
    assert "modify user set qoslevel=normal,small,medium,large" in s
    # QOS exist before the cluster that lists them
    assert s.index("add qos name=small") < s.index("add cluster")


def test_sacctmgr_equal_shares_and_all_associations():
    s = sacctmgr_script(PAIRS)
    for a, u in PAIRS:
        assert f"add user name={u} DefaultAccount=" in s
        assert f"Account={a} Fairshare=1" in s
    for a in ["acct1", "acct2", "acct3"]:
        assert f"add account name={a} Fairshare=1" in s
    # u2 keeps one default account across both associations
    assert s.count("name=u2 DefaultAccount=acct2") == 2


@pytest.mark.parametrize("text, seconds", [
    ("00:05:00", 300), ("05:00", 300), ("1-00:00:00", 86400), ("7-00:00:00", 604800),
])
def test_slurm_seconds(text, seconds):
    assert slurm_seconds(text) == seconds


@pytest.mark.parametrize("text, mb", [("120000M", 120000), ("62.50G", 64000), ("1T", 1048576)])
def test_mem_units(text, mb):
    assert mem_mb(text) == pytest.approx(mb)


def test_prepare_run_renders_everything(tmp_path):
    run = Path("results/_test/run")
    out = prepare_run(run, ROOT / "sim/smoke/events.txt", ROOT / "sim/smoke/accounts.txt",
                      ROOT / "config/priority/B0.conf", extra_conf=["DebugFlags=Priority"])
    try:
        conf = (out / "etc/slurm.conf").read_text()
        for key in ["@ETC@", "@LOG@", "@VAR@", "@EVENTS@", "@TIME_START@"]:
            assert key not in conf
        assert "Include /work/results/_test/run/etc/priority_weights.conf" in conf
        assert "NodeName=n[01-16] CPUs=32" in conf and conf.rstrip().endswith("DebugFlags=Priority")
        assert (out / "etc/slurmdbd.conf").stat().st_mode & 0o777 == 0o600
        assert len(parse_events(out / "events.txt")) == 36
        assert len(read_accounts(ROOT / "sim/smoke/accounts.txt")) == 6
    finally:
        import shutil
        shutil.rmtree(ROOT / "results/_test")


def test_b1_static_keys_replace_the_template():
    import json
    import shutil
    run = Path("results/_test/b1")
    out = prepare_run(run, ROOT / "sim/smoke/events.txt", ROOT / "sim/smoke/accounts.txt",
                      ROOT / "config/priority/B1.conf")
    try:
        conf = (out / "etc/slurm.conf").read_text()
        lines = conf.splitlines()
        for key, value in [("PriorityDecayHalfLife", "14-0"), ("PriorityMaxAge", "14-0"),
                           ("PriorityFavorSmall", "NO"), ("PriorityWeightPartition", "1000"),
                           ("PriorityCalcPeriod", "1")]:
            assert [l for l in lines if l.startswith(f"{key}=")] == [f"{key}={value}"]
        assert not any(l.startswith("PriorityFlags=") for l in lines)
        weights = (out / "etc/priority_weights.conf").read_text()
        assert "PriorityWeightFairshare=10000" in weights and "PriorityWeightAge=1000" in weights
        # the other configurations keep the template's keys
        c0 = prepare_run(Path("results/_test/c0"), ROOT / "sim/smoke/events.txt",
                         ROOT / "sim/smoke/accounts.txt", ROOT / "config/priority/C0.conf")
        assert "PriorityFlags=SMALL_RELATIVE_TO_TIME" in (c0 / "etc/slurm.conf").read_text()
    finally:
        shutil.rmtree(ROOT / "results/_test")


def test_apply_static_rejects_unknown_key(tmp_path):
    from sim.prepare_run import apply_static
    f = tmp_path / "x.static"
    f.write_text("PriorityNoSuchKey=1\n")
    with pytest.raises(ValueError, match="appears 0 times"):
        apply_static("PriorityMaxAge=1-0\n", f)


@pytest.mark.parametrize("variant,check", [
    ("triggers.yaml", lambda p: p["dwell_s"] == 3600 and "signal" not in p),
    ("triggers_A2.yaml", lambda p: p["dwell_s"]["small"] == 7200 and p["dwell_s"]["large"] == 3600),
    ("triggers_A3.yaml", lambda p: p["signal"] == "arrivals" and p["window_s"] == 3600
                                   and p["dwell_s"] == 3600),
])
def test_trigger_variants_reach_params(variant, check):
    import json
    import shutil
    from controller.policy import Params
    run = Path("results/_test/adaptive")
    out = prepare_run(run, ROOT / "sim/smoke/events.txt", ROOT / "sim/smoke/accounts.txt",
                      ROOT / "config/priority/C0.conf", policy="adaptive",
                      triggers=ROOT / "config" / variant)
    try:
        params = json.loads((out / "policy/params.json").read_text())
        assert check(params)
        Params.from_dict(params)
        assert len(params["quantiles"]["small"]) == 101
    finally:
        shutil.rmtree(ROOT / "results/_test")


def test_batch_run_names_match_run_sim():
    import yaml
    from sim.batch import EVAL, _mem_mb, container_name, run_dir_for
    cfg = yaml.safe_load(EVAL.read_text())
    assert str(run_dir_for(cfg, "X-s2", "A3", "matrix")) == "results/phase3/runs/X-s2/A3"
    assert str(run_dir_for(cfg, "N-s1", "C0", "pilot")) == "results/phase3/runs/N-s1/C0-pilot"
    # sim/run_sim.sh: "slurmsim-" + path with '/' -> '-' and other characters dropped
    assert container_name(Path("results/phase3/runs/N-s1/C0-pilot/")) == \
        "slurmsim-results-phase3-runs-N-s1-C0-pilot"
    assert _mem_mb("1.5GiB") == 1536 and _mem_mb("300MiB") == 300 and _mem_mb("--") == 0


def test_evaluation_config_is_consistent():
    import yaml
    from sim.batch import EVAL
    cfg = yaml.safe_load(EVAL.read_text())
    assert set(cfg["policies"]) == {"B1", "C0", "A1", "A2", "A3"}   # SPEC §8.2 (2026-11-27)
    assert len(cfg["workloads"]) == 9
    for pid, pol in cfg["policies"].items():
        assert (ROOT / "config/priority" / f"{pol['priority']}.conf").exists(), pid
        assert (ROOT / "config/priority" / f"{pol['priority']}.qos").exists(), pid
        if pol["kind"] == "adaptive":
            assert (ROOT / "config" / pol["triggers"]).exists(), pid
    for w in cfg["workloads"] + cfg["repeats"]["workloads"]:
        assert w.split("-")[0] in cfg["time_start"]
    assert set(cfg["repeats"]["policies"]) <= set(cfg["policies"])


def test_rejected_submissions_are_counted(tmp_path):
    from sim.check_run import rejected_submissions
    (tmp_path / "out").mkdir()
    (tmp_path / "out/slurmctld.log").write_text(
        "[2024-07-02T10:00:00] error: job_allocate: MaxJobCount limit from slurm.conf reached (10000)\n"
        "[2024-07-02T10:00:00] error: SBATCH: Batch job submission failed: Unexpected message received\n"
        "[2024-07-02T10:00:01] error: job_allocate: MaxJobCount limit from slurm.conf reached (10000)\n"
        "[2024-07-02T10:00:01] error: SBATCH: Batch job submission failed: Unexpected message received\n"
        "[2024-07-02T10:00:02] sched: Allocate JobId=1001\n")
    assert rejected_submissions(tmp_path) == {
        "MaxJobCount limit from slurm.conf reached (N)": 2, "submission failed": 2}


def test_slurm_conf_holds_every_job_of_the_largest_workload():
    import re
    import pandas as pd
    conf = (ROOT / "sim/etc/slurm.conf.in").read_text()
    limit = int(re.search(r"^MaxJobCount=(\d+)$", conf, re.M).group(1))
    largest = max(len(pd.read_parquet(f, columns=["job_id"]))
                  for f in (ROOT / "workloads/traces").glob("*/jobs.parquet"))
    assert limit >= 2 * largest


def test_batch_timeout_stops_the_container(monkeypatch):
    import subprocess
    from sim import batch
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0].endswith("run_sim.sh"):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(batch.subprocess, "run", fake_run)
    code, why = batch.simulate(Path("results/phase3/runs/X-s1/C0"), 60, 3600)
    assert code is None and "did not finish within 1 h" in why
    assert calls[-1] == ["docker", "rm", "-f", "slurmsim-results-phase3-runs-X-s1-C0"]


def _fake_run(tmp_path, elapsed, state="COMPLETED"):
    """A one-node run dir with len(elapsed) jobs of 1000 s run time and a 60-min limit."""
    (tmp_path / "out").mkdir(exist_ok=True)
    ev, rows = [], ["JobID|JobIDRaw|Account|User|Submit|Start|End|Elapsed|State|NNodes|NCPUS|"
                    "ReqMem|Timelimit|QOS|NodeList|JobName"]
    for i, e in enumerate(elapsed):
        name = f"jobid_{1001 + i}"
        ev.append(f"-dt {i} -e submit_batch_job | -J {name} -sim-walltime 1000 --uid=u1 -t 60 "
                  f"-n 1 --ntasks-per-node=1 -N 1 --mem=1000 -A a1 -p normal -q small pseudo.job")
        st = state if isinstance(state, str) else state[i]
        rows.append(f"{1001 + i}|{1001 + i}|a1|u1|2024-01-01T00:00:{i:02d}|2024-01-01T00:00:{i:02d}|"
                    f"2024-01-01T01:00:00|00:{e // 60:02d}:{e % 60:02d}|{st}|1|1|1000M|01:00:00|small|n01|{name}")
    (tmp_path / "events.txt").write_text("\n".join(ev) + "\n")
    (tmp_path / "out/slurm_acct.out").write_text("\n".join(rows) + "\n")
    (tmp_path / "out/slurmctld.log").write_text("")
    return tmp_path


def test_a_few_early_finishes_are_a_known_issue(tmp_path):
    from sim.check_run import KNOWN_ISSUES, check
    run = _fake_run(tmp_path, [1000, 1000, 400])           # one job 600 s short
    assert check(run) == []
    assert KNOWN_ISSUES and "completed early by the simulator (600-600 s short)" in KNOWN_ISSUES[0]


def test_many_early_finishes_or_late_runs_fail(tmp_path):
    from sim.check_run import EARLY_FINISH_MAX, check
    run = _fake_run(tmp_path / "a", [400] * (EARLY_FINISH_MAX + 1)) if (tmp_path / "a").mkdir() is None else None
    assert any("run time differs" in p for p in check(run))
    late = _fake_run(tmp_path / "b", [1000, 1500]) if (tmp_path / "b").mkdir() is None else None
    assert any("run time differs" in p for p in check(late))


def test_completions_a_few_seconds_late_are_a_known_issue(tmp_path):
    from sim.check_run import KNOWN_ISSUES, LATE_TOLERANCE_S, check
    ok = _fake_run(tmp_path / "a", [1000, 1003, 1005]) if (tmp_path / "a").mkdir() is None else None
    assert check(ok) == [] and "completed 3-5 s late" in KNOWN_ISSUES[0]
    bad = _fake_run(tmp_path / "b", [1000, 1000 + LATE_TOLERANCE_S + 1]) if (tmp_path / "b").mkdir() is None else None
    assert any("run time differs" in p for p in check(bad))


def test_submissions_a_few_seconds_late_are_a_known_issue(tmp_path):
    from sim.check_run import KNOWN_ISSUES, check
    run = _fake_run(tmp_path, [1000, 1000])
    acct = (run / "out/slurm_acct.out").read_text().replace(
        "2024-01-01T00:00:01|2024-01-01T00:00:01", "2024-01-01T00:00:05|2024-01-01T00:00:05")
    (run / "out/slurm_acct.out").write_text(acct)               # second job 4 s late
    assert check(run) == [] and any("submitted 4-4 s late" in k for k in KNOWN_ISSUES)


def test_batch_spaces_out_starts(monkeypatch):
    from sim import batch
    slept = []
    monkeypatch.setattr(batch.time, "sleep", lambda s: slept.append(s))
    batch._last_start[0] = batch.time.time()
    batch.wait_start_slot(20)
    assert slept and 19 < slept[0] <= 20


def test_refused_submissions_reads_the_container_log(monkeypatch):
    import subprocess
    from sim import batch
    monkeypatch.setattr(batch.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "19836\n", ""))
    assert batch.refused_submissions("slurmsim-x") == 19836
    monkeypatch.setattr(batch.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such container"))
    assert batch.refused_submissions("slurmsim-x") == 0


def test_a_job_killed_at_its_limit_a_few_seconds_late_is_a_known_issue(tmp_path):
    from sim.check_run import KNOWN_ISSUES, check
    run = _fake_run(tmp_path, [1000, 3602], state=["COMPLETED", "TIMEOUT"])
    ev = (run / "events.txt").read_text().splitlines()
    ev[1] = ev[1].replace("-sim-walltime 1000", "-sim-walltime 3600")     # runs to its 60-min limit
    (run / "events.txt").write_text("\n".join(ev) + "\n")
    assert check(run) == [] and "completed 2-2 s late" in KNOWN_ISSUES[0]


def test_adaptive_states_must_share_the_starting_static_keys(tmp_path):
    """A C0-based policy cannot switch to B1L: its static keys (B1's) differ (SPEC §9.2)."""
    import shutil
    import yaml
    trig = yaml.safe_load((ROOT / "config/triggers.yaml").read_text())
    trig["state_config"] = {**trig["state_config"], "large": "B1L"}
    f = tmp_path / "triggers_mixed.yaml"
    f.write_text(yaml.safe_dump(trig))
    from sim.prepare_run import static_keys
    assert static_keys("C0") is None and static_keys("B1L") == static_keys("B1M") == static_keys("B1")
    try:
        with pytest.raises(ValueError, match="static priority keys"):
            prepare_run(Path("results/_test/mixed"), ROOT / "sim/smoke/events.txt",
                        ROOT / "sim/smoke/accounts.txt", ROOT / "config/priority/C0.conf",
                        policy="adaptive", triggers=f)
    finally:
        shutil.rmtree(ROOT / "results/_test", ignore_errors=True)


def _snapshots(medium_boost=5000.0):
    """One B1M snapshot: two medium jobs (boosted) and small jobs with B1 priorities around."""
    import pandas as pd
    t = pd.Timestamp("2024-07-08 03:00")
    rows = []
    for i, (qos, base) in enumerate([("medium", 1000), ("medium", 2500), ("small", 4000),
                                     ("small", 7000)] + [("small", 100)] * 20):
        qv = medium_boost if qos == "medium" else 0.0
        rows.append({"snapshot": t, "state": "B1M", "spell": 1, "job_id": i, "qos": qos,
                     "QOS_v": qv, "priority": base + qv, "Age_w": 1000.0,
                     "Fairshare_w": 10000.0, "JobSize_w": 1000.0, "QOS_w": 5000.0})
    return pd.DataFrame(rows)


def test_states_check_accepts_a_correct_boost():
    from sim.phase4_checks import check_states_and_order
    r = check_states_and_order(_snapshots())
    assert r["ok"] and r["queue_ok"] and r["cross_class_pairs"] == 2 * 22
    s = r["per_snapshot"][0]
    # the small job at 7000 leads the medium job at 1000 by more than the boost (6000), so
    # that one pair may stay behind; the medium job at 2500 (gap 4500) must get ahead
    assert s["violations"] == 0 and s["pairs_within_boost"] == 2 * 22 - 1
    assert r["favored_ahead_share"] == pytest.approx(43 / 44)


def test_states_check_flags_a_missing_boost():
    from sim.phase4_checks import check_states_and_order
    r = check_states_and_order(_snapshots(medium_boost=0.0))
    assert not r["ok"] and r["violations"]


def test_state_at_skips_the_minute_of_a_switch():
    import pandas as pd
    from sim.phase4_checks import state_at
    sw = [(pd.Timestamp("2024-07-08 02:00:30"), "B1", "B1L")]
    assert state_at(sw, pd.Timestamp("2024-07-08 01:50")) == "B1"
    assert state_at(sw, pd.Timestamp("2024-07-08 02:00")) is None
    assert state_at(sw, pd.Timestamp("2024-07-08 02:10")) == "B1L"


def test_setup_config_is_consistent():
    import yaml
    from sim.batch import batch_jobs
    from sim.prepare_run import load_triggers, resolve_triggers
    from controller.policy import Params
    cfg = yaml.safe_load((ROOT / "config/evaluation_setup.yaml").read_text())
    assert len(cfg["workloads"]) == 12 and cfg["reference"] == "B1"
    dropped = ("D-lglong", "D-flood", "D-mem")
    assert not any(w.startswith(dropped) for w in cfg["workloads"] + cfg["repeats"]["workloads"])
    for name, pol in cfg["policies"].items():
        assert (ROOT / "config/priority" / f"{pol['priority']}.conf").exists()
        if pol["kind"] == "adaptive":
            trig = resolve_triggers(load_triggers(ROOT / "config" / pol["triggers"]),
                                    pol.get("overrides"))
            assert trig["state_config"]["base"] == pol["priority"] == "B1"
            Params.from_dict({**trig, "initial_state": trig["initial_state"]})
    st = cfg["stages"][1]
    assert set(st["policies"]) <= set(cfg["policies"]) and set(st["adaptive"]) <= set(st["policies"])
    assert set(cfg["repeats"]["policies"]) <= set(st["policies"])
    jobs = batch_jobs(cfg, cfg["workloads"], st["policies"], "matrix", True)
    assert len(jobs) == 76 and len(set(jobs)) == 76              # SPEC §9.5, stage 1
