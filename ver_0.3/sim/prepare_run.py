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
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "sim" / "etc"
WEIGHTS = ROOT / "config" / "weights.yaml"
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


def prepare_run(run_dir: Path, events: Path, accounts: Path, priority: Path,
                time_start: int = 1_704_067_200, extra_conf: list[str] | None = None) -> Path:
    """extra_conf: lines appended to slurm.conf (e.g. 'DebugFlags=Priority')."""
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
    a = p.parse_args()
    print(prepare_run(a.run_dir, a.events, a.accounts, a.priority, a.time_start, a.extra_conf))


if __name__ == "__main__":
    main()
