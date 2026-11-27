"""Queue-state policy (SPEC §6.4): pick a configuration from the waiting queue's class mix.

Pure functions, standard library only: this runs inside the simulator container (Python
3.10, no third-party packages) as well as on the host for tests and replay.

Inputs per tick: simulated time and the number of waiting jobs per size class. The policy
keeps a small memory between ticks (current state, candidate streak, last switch time).
"""
from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field

CLASSES = ("small", "medium", "large")
# Tie-break order when two classes have the same percentile.
TIE_ORDER = ("large", "medium", "small")


@dataclass
class Params:
    n_min: int                         # fewer waiting jobs than this: hold
    p_enter: float                     # a class state starts at this percentile
    p_exit: float                      # a class state ends below this percentile
    persist_ticks: int                 # candidate must repeat this many ticks
    # Minimum simulated seconds in a state before leaving it (time since the last switch):
    # one value for every state, or a table {state: seconds} covering every state.
    dwell_s: int | dict[str, int]
    state_config: dict[str, str]       # state -> configuration id
    quantiles: dict[str, list[float]]  # class -> 101 share quantiles (q0..q100) from calibration
    initial_state: str = "mix"

    def __post_init__(self) -> None:
        if isinstance(self.dwell_s, dict):
            missing = sorted(set(self.state_config) - set(self.dwell_s))
            unknown = sorted(set(self.dwell_s) - set(self.state_config))
            if missing or unknown:
                raise ValueError(f"dwell_s table: missing states {missing}, unknown {unknown}")

    def dwell_for(self, state: str) -> int:
        """Minimum stay in `state` before the policy may leave it."""
        return int(self.dwell_s[state] if isinstance(self.dwell_s, dict) else self.dwell_s)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Memory:
    state: str = "mix"
    candidate: str | None = None
    streak: int = 0
    last_switch_t: int | None = None

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


def percentile_rank(share: float, quantiles: list[float]) -> int:
    """Highest percentile level k (0..100) whose calibration quantile is <= share."""
    k = bisect.bisect_right(quantiles, share) - 1
    return max(0, min(100, k))


def decide(t_sim: int, counts: dict[str, int], memory: Memory, params: Params
           ) -> tuple[Decision, Memory]:
    """One tick of SPEC §6.4. Returns the decision record and the updated memory."""
    counts = {c: int(counts.get(c, 0)) for c in CLASSES}
    n = sum(counts.values())
    mem = Memory(**asdict(memory))
    d = Decision(t_sim=t_sim, waiting=n, counts=counts, state_before=mem.state,
                 state=mem.state, config=params.state_config[mem.state])

    # 1. Gate: too few waiting jobs for priority to matter.
    if n < params.n_min:
        d.reason = f"hold: {n} waiting < {params.n_min}"
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
