"""Create a simulator run directory: rendered Slurm config, users, accounts and events.

Usage:
  .venv/bin/python -m sim.prepare_run RUN_DIR --events EVENTS --accounts ACCOUNTS \
      --priority config/priority/B0.conf [--time-start EPOCH]

RUN_DIR is relative to ver_0.3 (e.g. results/smoke/run1); it is mounted at /work in the
container. Layout written:
  RUN_DIR/etc/   slurm.conf, slurmdbd.conf, sim.conf, users.sim, sacctmgr.script,
                 priority_weights.conf
The QOS priorities come from the .qos file next to --priority (config/priority/<id>.qos).
  RUN_DIR/events.txt
Then sim/run_sim.sh RUN_DIR runs it; the runner's outputs go to RUN_DIR/out/.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "sim" / "etc"
WEIGHTS = ROOT / "config" / "weights.yaml"
PRIORITY_DIR = ROOT / "config" / "priority"
TRIGGERS = ROOT / "config" / "triggers.yaml"
CONTAINER_ROOT = Path("/work")
CONTAINER_LOG = "/home/slurm/simrun/log"
CONTAINER_VAR = "/home/slurm/simrun/var"
FIRST_UID = 2001


def read_accounts(path: Path) -> list[tuple[str, str]]:
    """accounts.txt: one 'account<TAB>user' association per line ('#' comments allowed)."""
    pairs = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            account, user = line.split("\t")
            pairs.append((account, user))
    return pairs


def users_sim(pairs: list[tuple[str, str]]) -> str:
    users = sorted({u for _, u in pairs})
    lines = [f"slurm:1000:slurm:1000"]
    lines += [f"{u}:{FIRST_UID + i}:users:100" for i, u in enumerate(users)]
    return "\n".join(lines) + "\n"


def size_qos_names() -> list[str]:
    """The size-class QOS labels (config/weights.yaml: size_qos)."""
    return list(yaml.safe_load(WEIGHTS.read_text())["size_qos"].values())


def read_qos(path: Path | None) -> dict[str, int]:
    """QOS priorities from a config/priority/<id>.qos file ('qos priority' per line).

    Without a file every size QOS gets priority 0.
    """
    pri = {q: 0 for q in size_qos_names()}
    if path is not None and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                q, v = line.split()
                if q not in pri:
                    raise ValueError(f"unknown QOS {q} in {path}")
                pri[q] = int(v)
    return pri


def sacctmgr_script(pairs: list[tuple[str, str]], qos_priority: dict[str, int] | None = None,
                    cluster: str = "sim16") -> str:
    """Equal shares (SPEC §2.3) and the size-class QOS with this configuration's priorities.

    Every account has RawShares=1 under root and every user RawShares=1. The size QOS have
    identical (unset) limits; they only carry priority. Every user may use every QOS.
    """
    qos_priority = qos_priority if qos_priority is not None else read_qos(None)
    qos_list = ",".join(["normal", *qos_priority])
    accounts = sorted({a for a, _ in pairs})
    first_account: dict[str, str] = {}
    for a, u in pairs:
        first_account.setdefault(u, a)
    out = ["modify QOS set normal Priority=0"]
    out += [f"add qos name={q} Priority={v}" for q, v in qos_priority.items()]
    out.append(f"add cluster Name={cluster} Fairshare=1 QOS={qos_list}")
    out += [f"add account name={a} Fairshare=1" for a in accounts]
    out.append("add user name=slurm DefaultAccount=root AdminLevel=Administrator")
    for a, u in sorted(set(pairs), key=lambda p: (p[1], p[0])):
        out.append(f"add user name={u} DefaultAccount={first_account[u]} Account={a} Fairshare=1")
    out.append(f"modify user set qoslevel={qos_list}")
    out.append("list associations format=Account,Cluster,User,Fairshare,QOS tree withd")
    out.append("list qos format=Name,Priority")
    return "\n".join(out) + "\n"


def render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    if "@" in "".join(l for l in text.splitlines() if not l.lstrip().startswith("#")):
        raise ValueError(f"unrendered placeholder in {template}")
    return text


def load_triggers(path: Path) -> dict:
    """A triggers file. One with `base: <file>` is an overlay on that file (same folder):
    its keys replace the base's (SPEC §8.2 variants A2, A3)."""
    path = Path(path)
    trig = yaml.safe_load(path.read_text())
    if "base" in trig:
        base = load_triggers(path.parent / trig.pop("base"))
        trig = {**base, **trig}
    return trig


PARAM_KEYS = ("n_min", "p_enter", "p_exit", "persist_ticks", "dwell_s", "state_config",
              "quantiles", "signal", "window_s", "n_min_arrivals",
              "candidates", "targets_s", "min_share", "active_s", "stable_s")


def resolve_triggers(trig: dict, overrides: dict | None = None) -> dict:
    """Apply per-run overrides, then turn A4's target_level into targets_s (SPEC §9.3)."""
    trig = {**trig, **(overrides or {})}
    if "target_level" in trig:
        levels = trig.get("target_levels", {})
        if trig["target_level"] not in levels:
            raise ValueError(f"target_level {trig['target_level']!r} not in {sorted(levels)}")
        trig["targets_s"] = levels[trig["target_level"]]
    return trig


def apply_static(text: str, static_path: Path) -> str:
    """Replace the template's static priority keys with a setting's own (SPEC §8.2, B1).

    static_path holds 'Key=Value' lines; an empty value removes the key. Each key must
    appear exactly once in the template, so nothing is silently added or duplicated.
    """
    lines = text.splitlines()
    for raw in Path(static_path).read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        hits = [i for i, l in enumerate(lines) if l.startswith(f"{key}=")]
        if len(hits) != 1:
            raise ValueError(f"{static_path}: {key} appears {len(hits)} times in slurm.conf")
        if value.strip():
            lines[hits[0]] = f"{key}={value.strip()}"
        else:
            del lines[hits[0]]
    return "\n".join(lines) + "\n"


def static_keys(config: str) -> tuple[str, ...] | None:
    """The static priority keys a configuration needs (its .static file), or None for the
    template's own. The hook cannot change them mid-run (SPEC §7.3)."""
    f = PRIORITY_DIR / f"{config}.static"
    if not f.exists():
        return None
    return tuple(l.strip() for l in f.read_text().splitlines()
                 if l.strip() and not l.lstrip().startswith("#"))


def write_policy_dir(run_dir: Path, c_run: Path, initial: str, adaptive: bool,
                     triggers: Path = TRIGGERS, overrides: dict | None = None) -> list[str]:
    """Set up RUN_DIR/policy for the simulator's policy hook (SPEC §7.2).

    Copies every configuration (config/priority/*.conf, *.qos) to policy/configs/. With
    adaptive, also writes policy/params.json from the triggers file (config/triggers.yaml,
    or a variant overlay such as config/triggers_A2.yaml) and policy/tick.sh, the command
    the simulator runs at each tick. Returns the sim.conf lines to add.
    """
    pol = run_dir / "policy"
    (pol / "configs").mkdir(parents=True)
    for f in sorted(PRIORITY_DIR.glob("*.conf")) + sorted(PRIORITY_DIR.glob("*.qos")):
        shutil.copy(f, pol / "configs" / f.name)
    lines = ["", "## ver_0.3 policy hook (SPEC §7)", f"PolicyDir = {c_run / 'policy'}",
             f"InitialConfig = {initial}"]
    if adaptive:
        trig = resolve_triggers(load_triggers(triggers), overrides)
        params = {k: trig[k] for k in PARAM_KEYS if k in trig}
        if "target_level" in trig:                 # recorded for traceability only
            params["target_level"] = trig["target_level"]
        if trig["state_config"].get(trig.get("initial_state", "mix")) != initial:
            raise ValueError(f"initial config {initial} does not match the initial state's "
                             f"config in {triggers}")
        # Every state must run under the static keys the run starts with (SPEC §9.2).
        base = static_keys(initial)
        for state, config in trig["state_config"].items():
            if static_keys(config) != base:
                raise ValueError(f"state {state} uses {config}, whose static priority keys "
                                 f"differ from {initial}'s; the hook cannot change them")
        params["initial_state"] = trig.get("initial_state", "mix")
        from controller.policy import Params
        Params.from_dict(params)   # fail here, not at every tick, on a bad dwell_s table
        (pol / "params.json").write_text(json.dumps(params, indent=1) + "\n")
        tick = pol / "tick.sh"
        tick.write_text("#!/bin/sh\n# Run by the simulator at every policy tick.\n"
                        f"cd /work && exec python3 -m controller.tick --dir {c_run / 'policy'}\n")
        tick.chmod(0o755)
        lines += [f"PolicyTickSeconds = {trig['tick_s']}", f"PolicyCommand = {c_run / 'policy' / 'tick.sh'}"]
    return lines


def prepare_run(run_dir: Path, events: Path, accounts: Path, priority: Path,
                time_start: int = 1_704_067_200, extra_conf: list[str] | None = None,
                policy: str | None = None, triggers: Path = TRIGGERS,
                trigger_overrides: dict | None = None) -> Path:
    """extra_conf: lines appended to slurm.conf (e.g. 'DebugFlags=Priority').

    policy: None (no hook), "scheduled" (configurations available for set_priority_config
    events) or "adaptive" (policy ticks too, with parameters from `triggers`, after
    `trigger_overrides`, e.g. {"target_level": "tight", "stable_s": 0} for A4). The run starts
    in the --priority configuration. If that configuration has a .static file (B1), its keys
    replace the template's static priority keys.
    """
    run_dir = (ROOT / run_dir).resolve()
    if ROOT not in run_dir.parents:
        raise ValueError("run directory must be inside ver_0.3")
    etc = run_dir / "etc"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    etc.mkdir(parents=True)
    c_run = CONTAINER_ROOT / run_dir.relative_to(ROOT)
    values = {"ETC": str(c_run / "etc"), "LOG": CONTAINER_LOG, "VAR": CONTAINER_VAR,
              "EVENTS": str(c_run / "events.txt"), "TIME_START": f"{float(time_start):.1f}"}
    for name in ["slurm.conf", "slurmdbd.conf", "sim.conf"]:
        (etc / name).write_text(render(TEMPLATES / f"{name}.in", values))
    static = Path(priority).with_suffix(".static")
    if static.exists():
        (etc / "slurm.conf").write_text(apply_static((etc / "slurm.conf").read_text(), static))
    if extra_conf:
        with open(etc / "slurm.conf", "a") as f:
            f.write("\n# Added for this run\n" + "\n".join(extra_conf) + "\n")
    (etc / "slurmdbd.conf").chmod(0o600)
    pairs = read_accounts(accounts)
    (etc / "users.sim").write_text(users_sim(pairs))
    qos_priority = read_qos(Path(priority).with_suffix(".qos"))
    (etc / "sacctmgr.script").write_text(sacctmgr_script(pairs, qos_priority))
    shutil.copy(priority, etc / "priority_weights.conf")
    shutil.copy(events, run_dir / "events.txt")
    if policy:
        if policy not in ("scheduled", "adaptive"):
            raise ValueError(f"unknown policy mode {policy}")
        lines = write_policy_dir(run_dir, c_run, Path(priority).stem, policy == "adaptive",
                                 triggers, trigger_overrides)
        with open(etc / "sim.conf", "a") as f:
            f.write("\n".join(lines) + "\n")
    return run_dir


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_dir", type=Path)
    p.add_argument("--events", type=Path, required=True)
    p.add_argument("--accounts", type=Path, required=True)
    p.add_argument("--priority", type=Path, required=True)
    p.add_argument("--time-start", type=int, default=1_704_067_200)
    p.add_argument("--extra-conf", action="append", default=[],
                   help="line to append to slurm.conf; repeatable")
    p.add_argument("--policy", choices=["scheduled", "adaptive"],
                   help="enable the policy hook: fixed-time switches only, or policy ticks")
    p.add_argument("--triggers", type=Path, default=TRIGGERS,
                   help="triggers file for --policy adaptive (default config/triggers.yaml)")
    a = p.parse_args()
    print(prepare_run(a.run_dir, a.events, a.accounts, a.priority, a.time_start, a.extra_conf,
                      a.policy, a.triggers))


if __name__ == "__main__":
    main()
