"""Queue-state policies. Pure functions, standard library only: this runs inside the simulator
container (Python 3.10, no third-party packages) as well as on the host for tests and replay.

Two rules, chosen by Params.signal:
  "waiting", "arrivals"  A1-A3 (SPEC §6.4, §8.3): pick a configuration from the class mix of
                         the waiting queue or of recent arrivals, with hysteresis.
  "wait_target"          A4 (SPEC §9.3): favor medium or large for a fixed spell when the
                         median wait of its waiting jobs reaches its target, then return to
                         the baseline and hold it for a stabilization period.
The policy keeps a small memory between ticks (state, candidate streak, last switch time,
and for A4 the phase and when it ends).
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field

CLASSES = ("small", "medium", "large")
# Tie-break order when two classes have the same percentile (or, for A4, the same ratio).
TIE_ORDER = ("large", "medium", "small")
SIGNALS = ("waiting", "arrivals", "wait_target")
PHASES = ("armed", "active", "stabilizing")
BASE = "base"                          # A4's baseline state


@dataclass
class Params:
    n_min: int                         # fewer waiting jobs than this: hold
    persist_ticks: int                 # candidate must repeat this many ticks
    state_config: dict[str, str]       # state -> configuration id
    # A1-A3 (signals "waiting" and "arrivals"):
    p_enter: float | None = None       # a class state starts at this percentile
    p_exit: float | None = None        # a class state ends below this percentile
    # Minimum simulated seconds in a state before leaving it (time since the last switch):
    # one value for every state, or a table {state: seconds} covering every state.
    dwell_s: int | dict[str, int] | None = None
    quantiles: dict[str, list[float]] | None = None   # class -> 101 share quantiles (q0..q100)
    initial_state: str = "mix"
    # What the shares are taken from: the waiting queue (SPEC §6.2) or the jobs submitted in
    # the last window_s seconds (SPEC §8.3). With arrivals, a tick also holds when fewer than
    # n_min_arrivals jobs arrived in the window.
    signal: str = "waiting"
    window_s: int = 3600
    n_min_arrivals: int = 20
    # A4 (signal "wait_target", SPEC §9.3):
    candidates: list[str] = field(default_factory=lambda: ["medium", "large"])
    targets_s: dict[str, int] | None = None   # class -> wait target (median waited_s)
    min_share: float = 0.10            # class gate: share of all waiting jobs
    active_s: int | dict[str, int] = 3600     # favored spell, one value or one per state
    stable_s: int = 3600               # stabilization period after each spell

    def __post_init__(self) -> None:
        if self.signal not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}, got {self.signal!r}")
        if self.n_min < 1 or self.n_min_arrivals < 1:
            raise ValueError("n_min and n_min_arrivals must be at least 1")
        if self.signal == "wait_target":
            self._check_wait_target()
            return
        if None in (self.p_enter, self.p_exit, self.dwell_s, self.quantiles):
            raise ValueError("signals 'waiting' and 'arrivals' need p_enter, p_exit, dwell_s "
                             "and quantiles")
        if isinstance(self.dwell_s, dict):
            missing = sorted(set(self.state_config) - set(self.dwell_s))
            unknown = sorted(set(self.dwell_s) - set(self.state_config))
            if missing or unknown:
                raise ValueError(f"dwell_s table: missing states {missing}, unknown {unknown}")

    def _check_wait_target(self) -> None:
        want = {BASE, *self.candidates}
        if set(self.state_config) != want or self.initial_state != BASE:
            raise ValueError(f"wait_target: state_config must cover exactly {sorted(want)} and "
                             f"the run must start in {BASE!r}")
        if not set(self.candidates) <= set(CLASSES):
            raise ValueError(f"wait_target: unknown candidates {self.candidates}")
        if not self.targets_s or set(self.targets_s) != set(self.candidates):
            raise ValueError("wait_target: targets_s must give one target per candidate")
        if any(v <= 0 for v in self.targets_s.values()) or not 0 <= self.min_share < 1:
            raise ValueError("wait_target: targets must be positive, min_share in [0, 1)")
        if isinstance(self.active_s, dict) and set(self.active_s) != set(self.candidates):
            raise ValueError("wait_target: an active_s table needs one value per candidate")
        if self.stable_s < 0 or min(self._active_values()) <= 0:
            raise ValueError("wait_target: active_s must be positive and stable_s >= 0")

    def _active_values(self) -> list[int]:
        a = self.active_s
        return [int(v) for v in a.values()] if isinstance(a, dict) else [int(a)]

    def dwell_for(self, state: str) -> int:
        """Minimum stay in `state` before the policy may leave it (A1-A3)."""
        return int(self.dwell_s[state] if isinstance(self.dwell_s, dict) else self.dwell_s)

    def active_for(self, state: str) -> int:
        """Length of a favored spell in `state` (A4)."""
        return int(self.active_s[state] if isinstance(self.active_s, dict) else self.active_s)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Memory:
    state: str = "mix"
    candidate: str | None = None
    streak: int = 0
    last_switch_t: int | None = None
    phase: str = "armed"               # A4 only: armed, active or stabilizing
    phase_end_t: int | None = None     # A4 only: when the active or stabilizing phase ends

    @classmethod
    def from_dict(cls, d: dict | None) -> "Memory":
        return cls(**(d or {}))


@dataclass
class Decision:
    t_sim: int
    waiting: int
    counts: dict[str, int]
    shares: dict[str, float] = field(default_factory=dict)
    percentiles: dict[str, int] = field(default_factory=dict)
    state_before: str = "mix"
    state: str = "mix"
    candidate: str | None = None
    streak: int = 0
    switch: bool = False
    config: str = ""
    reason: str = ""
    # A4 only
    median_wait_s: dict[str, float | None] = field(default_factory=dict)
    ratios: dict[str, float | None] = field(default_factory=dict)
    phase_before: str = ""
    phase: str = ""
    phase_left_s: int | None = None


def percentile_rank(share: float, quantiles: list[float]) -> int:
    """Highest percentile level k (0..100) whose calibration quantile is <= share."""
    k = bisect.bisect_right(quantiles, share) - 1
    return max(0, min(100, k))


def decide(t_sim: int, counts: dict[str, int], memory: Memory, params: Params,
           waiting: int | None = None, median_wait_s: dict | None = None
           ) -> tuple[Decision, Memory]:
    """One tick. Returns the decision record and the updated memory.

    counts: jobs per size class in the signal: waiting jobs (signals "waiting" and
    "wait_target"), or jobs submitted in the last window (signal "arrivals", which also needs
    `waiting`, the number of jobs waiting, for the gate). median_wait_s: per class, the median
    time the waiting jobs have waited (signal "wait_target"; None for a class with none).
    """
    if params.signal == "wait_target":
        if median_wait_s is None:
            raise ValueError("the wait_target signal needs the median wait per class")
        return decide_wait_target(t_sim, counts, median_wait_s, memory, params)
    counts = {c: int(counts.get(c, 0)) for c in CLASSES}
    n = sum(counts.values())
    if params.signal == "arrivals":
        if waiting is None:
            raise ValueError("the arrivals signal needs the number of waiting jobs")
        n_wait = int(waiting)
    else:
        n_wait = n
    mem = Memory(**asdict(memory))
    d = Decision(t_sim=t_sim, waiting=n_wait, counts=counts, state_before=mem.state,
                 state=mem.state, config=params.state_config[mem.state])

    # 1. Gate: too few waiting jobs for priority to matter (or, for arrivals, too few
    #    arrivals for their mix to mean much).
    gate = None
    if n_wait < params.n_min:
        gate = f"hold: {n_wait} waiting < {params.n_min}"
    elif params.signal == "arrivals" and n < params.n_min_arrivals:
        gate = f"hold: {n} arrivals < {params.n_min_arrivals}"
    if gate:
        d.reason = gate
        d.candidate, d.streak = mem.candidate, mem.streak
        return d, mem

    # 2. Candidate: the class with the highest percentile, if it reaches p_enter.
    d.shares = {c: round(counts[c] / n, 4) for c in CLASSES}
    d.percentiles = {c: percentile_rank(counts[c] / n, params.quantiles[c]) for c in CLASSES}
    top = max(TIE_ORDER, key=lambda c: d.percentiles[c])          # max keeps the first on ties
    cand = top if d.percentiles[top] >= params.p_enter else "mix"
    mem.streak = mem.streak + 1 if cand == mem.candidate else 1
    mem.candidate = cand
    d.candidate, d.streak = cand, mem.streak

    if cand == mem.state:
        d.reason = f"hold: candidate is the current state ({cand})"
        return d, mem

    # 3. Hysteresis.
    if mem.state == "mix":
        exit_ok, why = True, f"{cand} at P{d.percentiles[cand]} >= {params.p_enter:g}"
    else:
        cur = d.percentiles[mem.state]
        if cur < params.p_exit:
            exit_ok, why = True, f"{mem.state} fell to P{cur} < {params.p_exit:g}"
        elif cand != "mix" and d.percentiles[cand] > cur:
            exit_ok, why = True, f"{cand} at P{d.percentiles[cand]} above {mem.state} at P{cur}"
        else:
            exit_ok, why = False, f"{mem.state} still at P{cur} >= {params.p_exit:g}"
    persist_ok = mem.streak >= params.persist_ticks
    since = None if mem.last_switch_t is None else t_sim - mem.last_switch_t
    dwell = params.dwell_for(mem.state)
    dwell_ok = since is None or since >= dwell

    if exit_ok and persist_ok and dwell_ok:
        mem.state, mem.last_switch_t = cand, t_sim
        d.switch, d.state, d.config = True, cand, params.state_config[cand]
        d.reason = f"switch to {cand}: {why}; {mem.streak} ticks"
    else:
        blocks = [b for b, ok in [(why, exit_ok),
                                  (f"streak {mem.streak} < {params.persist_ticks}", persist_ok),
                                  (f"{since}s since switch < {dwell}", dwell_ok)] if not ok]
        d.reason = "hold: " + "; ".join(blocks)
    return d, mem


def decide_wait_target(t_sim: int, counts: dict[str, int], median_wait_s: dict,
                       memory: Memory, params: Params) -> tuple[Decision, Memory]:
    """One tick of SPEC §9.3 (A4).

    Phases: armed (baseline, the trigger is read), active (one class favored for a fixed
    spell), stabilizing (baseline, the queue is not read). At most one switch per tick:
      active       at the first tick at or after the spell's end: back to the baseline,
                   then stabilizing until stable_s later (with stable_s = 0, armed at once,
                   but the trigger is first read at the next tick);
      stabilizing  at the first tick at or after its end: armed, and the trigger is read
                   from this tick, with the streak starting again from zero;
      armed        gate: at least n_min jobs waiting. A candidate class makes up at least
                   min_share of the waiting jobs and its median wait is at or above its
                   target; with two, the larger median wait / target wins (ties: TIE_ORDER).
                   The same candidate on persist_ticks consecutive ticks starts its spell.
    """
    counts = {c: int(counts.get(c, 0)) for c in CLASSES}
    waits = {c: (None if median_wait_s.get(c) is None else float(median_wait_s[c]))
             for c in CLASSES}
    n = sum(counts.values())
    mem = Memory(**asdict(memory))
    d = Decision(t_sim=t_sim, waiting=n, counts=counts, state_before=mem.state, state=mem.state,
                 config=params.state_config[mem.state], median_wait_s=waits,
                 phase_before=mem.phase, phase=mem.phase,
                 shares={c: round(counts[c] / n, 4) if n else 0.0 for c in CLASSES})
    d.ratios = {c: (None if waits[c] is None else round(waits[c] / params.targets_s[c], 4))
                for c in params.candidates}

    def done(reason: str) -> tuple[Decision, Memory]:
        d.reason, d.candidate, d.streak = reason, mem.candidate, mem.streak
        d.phase = mem.phase
        d.phase_left_s = None if mem.phase_end_t is None else max(0, mem.phase_end_t - t_sim)
        return d, mem

    if mem.phase == "active":
        if t_sim < mem.phase_end_t:
            return done(f"hold: {mem.state} spell, {mem.phase_end_t - t_sim}s left")
        left = mem.state
        mem.state, mem.last_switch_t = BASE, t_sim
        mem.candidate, mem.streak = None, 0
        if params.stable_s > 0:
            mem.phase, mem.phase_end_t = "stabilizing", t_sim + params.stable_s
        else:
            mem.phase, mem.phase_end_t = "armed", None
        d.switch, d.state, d.config = True, BASE, params.state_config[BASE]
        return done(f"switch to {BASE}: {left} spell over; stabilizing {params.stable_s}s")

    if mem.phase == "stabilizing":
        if t_sim < mem.phase_end_t:
            return done(f"hold: stabilizing, {mem.phase_end_t - t_sim}s left")
        mem.phase, mem.phase_end_t = "armed", None
        mem.candidate, mem.streak = None, 0

    if n < params.n_min:
        return done(f"hold: {n} waiting < {params.n_min}")

    eligible = {}
    for c in params.candidates:
        if waits[c] is None or counts[c] < params.min_share * n:
            continue
        if waits[c] >= params.targets_s[c]:
            eligible[c] = waits[c] / params.targets_s[c]
    cand = None
    if eligible:
        top = max(eligible.values())
        cand = next(c for c in TIE_ORDER if eligible.get(c) == top)
    if cand is None:
        mem.candidate, mem.streak = None, 0
        return done("hold: no class past its target")
    mem.streak = mem.streak + 1 if cand == mem.candidate else 1
    mem.candidate = cand
    if mem.streak < params.persist_ticks:
        return done(f"hold: {cand} at {eligible[cand]:.2f}x its target; streak {mem.streak} "
                    f"< {params.persist_ticks}")
    spell = params.active_for(cand)
    mem.state, mem.last_switch_t = cand, t_sim
    mem.phase, mem.phase_end_t = "active", t_sim + spell
    d.switch, d.state, d.config = True, cand, params.state_config[cand]
    reason = (f"switch to {cand}: median wait {waits[cand]:.0f}s = {eligible[cand]:.2f}x its "
              f"target for {mem.streak} ticks; spell {spell}s")
    d.candidate, d.streak = cand, mem.streak
    mem.candidate, mem.streak = None, 0
    d.reason, d.phase, d.phase_left_s = reason, mem.phase, spell
    return d, mem
