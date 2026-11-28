"""Tests for the queue-state policy (SPEC §6.4), the tick command and replay (SPEC §7)."""
import json
from dataclasses import replace

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


def run_ticks(seq, memory=None, t0=0, step=600, params=PARAMS):
    memory = memory or Memory()
    out = []
    for i, counts in enumerate(seq):
        d, memory = decide(t0 + i * step, counts, memory, params)
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


PER_STATE = replace(PARAMS, dwell_s={"mix": 3600, "large": 3600, "small": 7200, "medium": 7200})


def test_per_state_minimum_stay():
    assert PER_STATE.dwell_for("small") == 7200 and PER_STATE.dwell_for("large") == 3600
    assert PARAMS.dwell_for("small") == 3600                       # one value for every state
    # 70 minutes into large (60-min stay): leaving is allowed
    mem = Memory(state="large", candidate="large", streak=5, last_switch_t=0)
    ds, _ = run_ticks([MIX, MIX], mem, t0=3600, params=PER_STATE)
    assert ds[1].switch and ds[1].state == "mix"
    # 70 minutes into small (120-min stay): held, then leaves once 120 minutes have passed
    drop = {"small": 72, "medium": 14, "large": 14}                # small at P8, large at P100
    mem = Memory(state="small", candidate="small", streak=5, last_switch_t=0)
    ds, mem = run_ticks([drop, drop], mem, t0=3600, params=PER_STATE)
    assert not ds[1].switch and ds[1].reason == "hold: 4200s since switch < 7200"
    ds, mem = run_ticks([drop], mem, t0=7200, params=PER_STATE)
    assert ds[0].switch and ds[0].state == "large" and mem.last_switch_t == 7200


def test_minimum_stay_table_must_name_every_state():
    with pytest.raises(ValueError, match="missing states \\['medium'\\]"):
        replace(PARAMS, dwell_s={"mix": 3600, "large": 3600, "small": 7200})
    with pytest.raises(ValueError, match="unknown \\['tiny'\\]"):
        replace(PARAMS, dwell_s={"mix": 1, "large": 1, "small": 1, "medium": 1, "tiny": 1})


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


# --- arrivals signal (SPEC §8.3) ---------------------------------------------------------

ARR = replace(PARAMS, signal="arrivals")


def test_arrivals_from_totals_uses_window_baseline():
    from controller.tick import arrivals_from_totals
    hist, out = [], []
    for k in range(1, 9):                       # ticks every 600 s; 10 small jobs per tick
        t = 600 * k
        c, hist = arrivals_from_totals(hist, t, {"small": 10 * k, "large": k}, window_s=3600)
        out.append(c)
    assert out[0] == {"small": 10, "medium": 0, "large": 1}     # since the start
    assert out[5] == {"small": 60, "medium": 0, "large": 6}     # t=3600: baseline is the start
    assert out[6] == {"small": 60, "medium": 0, "large": 6}     # t=4200: baseline t=600
    assert all(h[0] >= 4800 - 3600 for h in hist[1:]) and len(hist) <= 8


def test_arrivals_signal_gates_on_waiting_and_arrivals():
    d, _ = decide(0, LARGE, Memory(), ARR, waiting=10)
    assert d.reason == "hold: 10 waiting < 20" and d.waiting == 10
    d, _ = decide(0, {"small": 8, "medium": 1, "large": 1}, Memory(), ARR, waiting=50)
    assert d.reason == "hold: 10 arrivals < 20"
    with pytest.raises(ValueError, match="needs the number of waiting jobs"):
        decide(0, LARGE, Memory(), ARR)


def test_arrivals_signal_switches_on_arrival_mix():
    mem = Memory()
    for i in range(2):
        d, mem = decide(600 * i, LARGE, mem, ARR, waiting=50)   # arrivals: large at P100
    assert d.switch and d.state == "large" and d.waiting == 50 and d.counts == LARGE


def test_tick_with_arrivals_and_replay(tmp_path):
    from dataclasses import asdict
    (tmp_path / "params.json").write_text(json.dumps(asdict(ARR)))
    total = {"small": 0, "medium": 0, "large": 0}
    outs = []
    for i, step in enumerate([MIX, LARGE, LARGE, LARGE]):     # arrivals per tick
        total = {q: total[q] + step[q] for q in total}
        waiting = [{"id": j, "qos": "small"} for j in range(30)]
        (tmp_path / "state.json").write_text(json.dumps(
            {"t_sim": 600 * i, "config": "C0", "submitted": total, "waiting": waiting, "nodes": []}))
        outs.append(tick_run(tmp_path)["config"])
    log = [json.loads(l) for l in (tmp_path / "controller.jsonl").read_text().splitlines()]
    assert log[0]["signal"] == "arrivals" and log[0]["waiting"] == 30
    assert log[0]["waiting_counts"] == {"small": 30, "medium": 0, "large": 0}
    assert log[1]["counts"] == {q: MIX[q] + LARGE[q] for q in MIX}     # window covers both ticks
    assert "C1" in outs
    assert replay(tmp_path) == []


def test_tick_arrivals_needs_counter_in_snapshot(tmp_path):
    from dataclasses import asdict
    (tmp_path / "params.json").write_text(json.dumps(asdict(ARR)))
    (tmp_path / "state.json").write_text(json.dumps({"t_sim": 0, "config": "C0", "waiting": []}))
    with pytest.raises(ValueError, match="no 'submitted' totals"):
        tick_run(tmp_path)


def test_signal_must_be_known():
    with pytest.raises(ValueError, match="signal must be"):
        replace(PARAMS, signal="queue")


# --------------------------------------------------------------------------- A4 (SPEC §9.3)

H = 3600
A4 = Params(n_min=20, persist_ticks=2, signal="wait_target", initial_state="base",
            state_config={"base": "B1", "medium": "B1M", "large": "B1L"},
            candidates=["medium", "large"], targets_s={"medium": 4 * H, "large": 12 * H},
            min_share=0.10, active_s=H, stable_s=H)
QUEUE = {"small": 60, "medium": 25, "large": 15}          # medium 25%, large 15% of 100


def waits(medium=None, large=None, small=600.0):
    return {"small": small, "medium": medium, "large": large}


def a4_ticks(seq, params=A4, memory=None, t0=0, step=600):
    memory = memory or Memory(state="base")
    out = []
    for i, (counts, w) in enumerate(seq):
        d, memory = decide(t0 + i * step, counts, memory, params, median_wait_s=w)
        out.append(d)
    return out, memory


def test_a4_starts_a_spell_after_two_ticks_past_target():
    ds, mem = a4_ticks([(QUEUE, waits(medium=5 * H))] * 2)
    assert not ds[0].switch and "streak 1 < 2" in ds[0].reason and ds[0].phase == "armed"
    assert ds[1].switch and ds[1].state == "medium" and ds[1].config == "B1M"
    assert ds[1].phase == "active" and ds[1].phase_left_s == H and ds[1].ratios["medium"] == 1.25
    assert mem.phase == "active" and mem.phase_end_t == 600 + H


def test_a4_below_target_or_too_small_a_share_does_not_trigger():
    ds, _ = a4_ticks([(QUEUE, waits(medium=3 * H, large=11 * H))] * 3)      # under both targets
    assert not any(d.switch for d in ds) and ds[-1].reason == "hold: no class past its target"
    few = {"small": 95, "medium": 5, "large": 0}                            # medium 5% < 10%
    ds, _ = a4_ticks([(few, waits(medium=10 * H))] * 3)
    assert not any(d.switch for d in ds)


def test_a4_whole_queue_gate():
    ds, mem = a4_ticks([({"small": 10, "medium": 5, "large": 4}, waits(medium=9 * H))] * 3)
    assert not any(d.switch for d in ds) and ds[0].reason == "hold: 19 waiting < 20"


def test_a4_larger_ratio_wins_and_ties_go_to_large():
    ds, _ = a4_ticks([(QUEUE, waits(medium=5 * H, large=18 * H))] * 2)      # 1.25x vs 1.5x
    assert ds[1].state == "large"
    ds, _ = a4_ticks([(QUEUE, waits(medium=8 * H, large=24 * H))] * 2)      # 2x each
    assert ds[1].state == "large"


def test_a4_spell_is_fixed_then_returns_and_stabilizes():
    hot, cold = (QUEUE, waits(medium=5 * H)), (QUEUE, waits(medium=0, large=30 * H))
    seq = [hot, hot] + [cold] * 5 + [hot] * 8 + [hot, hot]
    ds, _ = a4_ticks(seq)
    t = [d.t_sim for d in ds]
    assert ds[1].switch and ds[1].state == "medium"                        # t = 600
    # no early exit when the wait falls, and no switch to large although it is far past target
    assert all(d.state == "medium" and not d.switch for d in ds[2:7])
    ret = next(i for i, d in enumerate(ds) if d.switch and d.state == "base")
    assert t[ret] == 600 + H and ds[ret].phase == "stabilizing" and ds[ret].config == "B1"
    # stabilizing: the queue is not read, even with medium past its target
    stab = [d for d in ds if d.phase == "stabilizing" and not d.switch]
    assert stab and all("stabilizing" in d.reason for d in stab)
    armed = next(i for i in range(ret + 1, len(ds)) if ds[i].phase_before == "stabilizing"
                 and ds[i].phase == "armed")
    assert t[armed] == t[ret] + H and not ds[armed].switch and ds[armed].streak == 1
    assert ds[armed + 1].switch and ds[armed + 1].state == "medium"         # streak from zero


def test_a4_without_stabilization_rearms_at_once():
    p = replace(A4, stable_s=0)
    ds, _ = a4_ticks([(QUEUE, waits(medium=5 * H))] * 12, params=p)
    ret = next(i for i, d in enumerate(ds) if d.switch and d.state == "base")
    assert ds[ret].phase == "armed"
    assert not ds[ret + 1].switch and ds[ret + 1].streak == 1
    assert ds[ret + 2].switch and ds[ret + 2].state == "medium"


def test_a4_spell_length_per_state():
    p = replace(A4, active_s={"medium": 2 * H, "large": H})
    ds, _ = a4_ticks([(QUEUE, waits(medium=5 * H))] * 16, params=p)
    on = next(i for i, d in enumerate(ds) if d.switch and d.state == "medium")
    off = next(i for i, d in enumerate(ds) if d.switch and d.state == "base")
    assert ds[off].t_sim - ds[on].t_sim == 2 * H


@pytest.mark.parametrize("bad", [
    {"targets_s": None},                                              # no targets
    {"targets_s": {"medium": 4 * H}},                                 # one missing
    {"state_config": {"mix": "C0", "medium": "B1M", "large": "B1L"}},  # no base state
    {"initial_state": "mix"},
    {"min_share": 1.5},
    {"active_s": {"medium": H}},                                      # table missing large
])
def test_a4_params_are_checked(bad):
    from dataclasses import asdict
    with pytest.raises(ValueError):
        Params(**{**asdict(A4), **bad})


def test_a4_tick_command_and_replay(tmp_path):
    from dataclasses import asdict
    from controller.tick import median_waits_from_snapshot
    (tmp_path / "params.json").write_text(json.dumps(asdict(A4)))
    def snap(t, med_wait):
        jobs = ([{"id": i, "qos": "small", "waited_s": 60} for i in range(60)] +
                [{"id": 100 + i, "qos": "medium", "waited_s": med_wait + 60 * (i - 12)} for i in range(25)] +
                [{"id": 200 + i, "qos": "large", "waited_s": 3600} for i in range(15)])
        return {"t_sim": t, "config": "B1", "waiting": jobs}
    s = snap(0, 5 * H)
    assert median_waits_from_snapshot(s) == {"small": 60.0, "medium": 5 * H, "large": 3600.0}
    assert median_waits_from_snapshot({"waiting": []})["large"] is None
    for i in range(20):
        (tmp_path / "state.json").write_text(json.dumps(snap(600 * i, 5 * H if i % 7 < 4 else 0)))
        tick_run(tmp_path)
    log = [json.loads(l) for l in (tmp_path / "controller.jsonl").read_text().splitlines()]
    assert sum(r["switch"] for r in log) >= 2 and {r["phase"] for r in log} >= {"armed", "active"}
    assert replay(tmp_path) == []


def test_a4_spell_statistics():
    from dataclasses import asdict
    from controller.summarize import a4_spells
    hot = (QUEUE, waits(medium=5 * H))
    ds, _ = a4_ticks([hot] * 30)                     # the trigger holds throughout
    log = [asdict(d) for d in ds]
    s = a4_spells(log, log[-1]["t_sim"], A4.persist_ticks)
    g = s["by_state"]["medium"]
    # spell 1 starts at tick 1; every later one at the first chance after stabilizing
    assert g["spells"] == len([d for d in ds if d.switch and d.state == "medium"]) >= 2
    assert g["started_at_first_chance"] == g["spells"] - 1
    assert g["median_wait_at_trigger_min"] == 300.0
    assert set(s["time_share_by_phase"]) == {"armed", "active-medium", "stabilizing"}
