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
