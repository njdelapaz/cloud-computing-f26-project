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
    assert len(cfg["policies"]) == 9 and len(cfg["workloads"]) == 9
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
