"""Tests for the queue-state policy (SPEC §6.4), the tick command and replay (SPEC §7)."""
import json

import pytest

from controller.policy import Memory, Params, decide, percentile_rank
from controller.replay import replay
from controller.tick import run as tick_run


def lin(a, b):
    return [a + (b - a) * k / 100 for k in range(101)]


PARAMS = Params(
    n_min=20, p_enter=80, p_exit=60, persist_ticks=2, dwell_s=3600,
    state_config={"mix": "C0", "large": "C1", "small": "C2", "medium": "C3"},
    quantiles={"small": lin(0.70, 0.95), "medium": lin(0.03, 0.25),
               "large": [0.0] * 30 + lin(0.0, 0.06)[30:]},
)
MIX = {"small": 85, "medium": 12, "large": 3}          # ordinary queue: every class mid-range
LARGE = {"small": 80, "medium": 10, "large": 10}       # large share 0.10: above its range
MEDIUM = {"small": 70, "medium": 28, "large": 2}       # medium share 0.28: above its range


def run_ticks(seq, memory=None, t0=0, step=600):
    memory = memory or Memory()
    out = []
    for i, counts in enumerate(seq):
        d, memory = decide(t0 + i * step, counts, memory, PARAMS)
        out.append(d)
    return out, memory


def test_percentile_rank_ties_and_bounds():
    q = PARAMS.quantiles["large"]
    assert percentile_rank(0.0, q) == 29          # 30 quantiles tie at 0: highest level wins
    assert percentile_rank(-1, q) == 0
    assert percentile_rank(1.0, q) == 100
    assert percentile_rank(0.03, lin(0, 0.06)) == 50


def test_gate_holds_with_short_queue():
    d, mem = decide(0, {"small": 5, "medium": 3, "large": 2}, Memory(), PARAMS)
    assert not d.switch and d.config == "C0" and d.reason.startswith("hold: 10 waiting")
    assert mem == Memory()


def test_mix_queue_stays_neutral():
    ds, mem = run_ticks([MIX] * 5)
    assert not any(d.switch for d in ds) and mem.state == "mix"
    assert all(d.config == "C0" and d.candidate == "mix" for d in ds)


def test_enter_large_after_persistence():
    ds, mem = run_ticks([LARGE, LARGE])
    assert not ds[0].switch and "streak 1 < 2" in ds[0].reason
    assert ds[1].switch and ds[1].state == "large" and ds[1].config == "C1"
    assert mem.state == "large" and mem.last_switch_t == 600


def test_dwell_blocks_quick_second_switch():
    ds, mem = run_ticks([LARGE, LARGE, MEDIUM, MEDIUM, MEDIUM])   # switch at t=600
    assert ds[1].switch
    assert not ds[3].switch and "since switch" in ds[3].reason   # t=1800: 1200 s < 3600
    later, mem = run_ticks([MEDIUM], mem, t0=600 + 3600)
    assert later[0].switch and later[0].state == "medium" and later[0].config == "C3"


def test_stays_in_state_between_exit_and_enter():
    mem = Memory(state="large", candidate="large", streak=5, last_switch_t=0)
    # large share 0.045 -> about P75: below enter (80) but above exit (60)
    ds, mem = run_ticks([{"small": 840, "medium": 115, "large": 45}] * 3, mem, t0=10_000)
    assert all(d.candidate == "mix" and not d.switch for d in ds)
    assert "still at P" in ds[-1].reason and mem.state == "large"


def test_exit_when_percentile_drops():
    mem = Memory(state="large", candidate="large", streak=5, last_switch_t=0)
    ds, mem = run_ticks([MIX, MIX], mem, t0=10_000)
    assert not ds[0].switch and ds[1].switch
    assert ds[1].state == "mix" and ds[1].config == "C0" and "fell to P" in ds[1].reason


def test_takeover_by_higher_class():
    mem = Memory(state="large", candidate="large", streak=5, last_switch_t=0)
    both = {"small": 64, "medium": 31, "large": 5}     # large at P83 (>= 80), medium at P100
    ds, mem = run_ticks([both, both], mem, t0=10_000)
    assert ds[1].switch and ds[1].state == "medium"


def _write(tmp_path, counts, t, config="C0"):
    waiting = [{"id": i, "qos": q} for q, n in counts.items() for i in range(n)]
    (tmp_path / "state.json").write_text(json.dumps({"t_sim": t, "config": config,
                                                     "waiting": waiting, "nodes": []}))


def test_tick_command_and_replay(tmp_path):
    from dataclasses import asdict
    (tmp_path / "params.json").write_text(json.dumps(asdict(PARAMS)))
    outs = []
    for i, counts in enumerate([MIX, LARGE, LARGE, MIX]):
        _write(tmp_path, counts, 600 * i)
        outs.append(tick_run(tmp_path))
    assert [o["config"] for o in outs] == [None, None, "C1", None]
    assert json.loads((tmp_path / "memory.json").read_text())["state"] == "large"
    log = [json.loads(l) for l in (tmp_path / "controller.jsonl").read_text().splitlines()]
    assert len(log) == 4 and log[2]["switch"] and log[2]["counts"] == LARGE
    assert replay(tmp_path) == []


def test_tick_command_fails_cleanly_on_bad_snapshot(tmp_path):
    import subprocess, sys
    from dataclasses import asdict
    (tmp_path / "params.json").write_text(json.dumps(asdict(PARAMS)))
    (tmp_path / "state.json").write_text("{not json")
    r = subprocess.run([sys.executable, "-m", "controller.tick", "--dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "failed" in r.stderr
    assert not (tmp_path / "decision.json").exists()
