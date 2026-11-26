"""Nine-class job classification (SPEC §4), driven by config/classes.yaml."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "classes.yaml"


@lru_cache(maxsize=None)
def load_config(path: str | Path = CONFIG) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    expected = [f"{s}-{t}" for s in cfg["size"] for t in cfg["time_limit_s"]]
    if cfg["classes"] != expected:
        raise ValueError(f"classes {cfg['classes']} do not match size x time order {expected}")
    return cfg


def _bin(fits: list[np.ndarray], labels: list[str], what: str) -> np.ndarray:
    """First label whose 'fits' mask is true; error if none fits."""
    out = np.select(fits, labels, default="")
    bad = out == ""
    if bad.any():
        raise ValueError(f"{int(bad.sum())} job(s) exceed the largest {what} class")
    return out


def size_class(cpus, mem_mb, cfg: dict | None = None) -> np.ndarray:
    """'sm' / 'med' / 'lg' by footprint. f <= b  <=>  cpus <= b*cores and mem <= b*mem."""
    cfg = cfg or load_config()
    cpus = np.asarray(cpus, dtype=float)
    mem = np.asarray(mem_mb, dtype=float)
    cores, node_mem = cfg["node"]["cores"], cfg["node"]["mem_mb"]
    eps = 1e-9
    labels = list(cfg["size"])
    fits = [(cpus <= b * cores + eps) & (mem <= b * node_mem + eps) for b in cfg["size"].values()]
    return _bin(fits, labels, "size")


def time_class(timelimit_s, cfg: dict | None = None) -> np.ndarray:
    """'short' / 'med' / 'long' by requested time limit."""
    cfg = cfg or load_config()
    t = np.asarray(timelimit_s)
    labels = list(cfg["time_limit_s"])
    fits = [t <= b for b in cfg["time_limit_s"].values()]
    return _bin(fits, labels, "time limit")


def footprint(cpus, mem_mb, cfg: dict | None = None) -> np.ndarray:
    cfg = cfg or load_config()
    return np.maximum(np.asarray(cpus) / cfg["node"]["cores"],
                      np.asarray(mem_mb) / cfg["node"]["mem_mb"])


def classify(cpus, mem_mb, timelimit_s, cfg: dict | None = None) -> pd.Categorical:
    """Class name per job, as an ordered categorical in the SPEC §4.3 order."""
    cfg = cfg or load_config()
    names = np.char.add(np.char.add(size_class(cpus, mem_mb, cfg), "-"), time_class(timelimit_s, cfg))
    return pd.Categorical(names, categories=cfg["classes"], ordered=True)


def class_summary(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Per-class jobs, core-hours and memory-hours (counts and shares).

    df needs columns: class, cpus, mem_mb, runtime_s.
    """
    cfg = cfg or load_config()
    g = df.assign(core_h=df["cpus"] * df["runtime_s"] / 3600,
                  mem_gbh=df["mem_mb"] * df["runtime_s"] / 3600 / 1000)
    out = (g.groupby("class", observed=False)
             .agg(jobs=("cpus", "size"), core_h=("core_h", "sum"), mem_gbh=("mem_gbh", "sum"))
             .reindex(cfg["classes"]).fillna(0))
    for col in ["jobs", "core_h", "mem_gbh"]:
        total = out[col].sum()
        out[f"{col}_share"] = out[col] / total if total else 0.0
    out.index.name = "class"
    return out
