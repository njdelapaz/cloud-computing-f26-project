"""Task 3.6: figures for the one-week results (SPEC §8.8), from results/phase3/metrics.csv.

Usage: .venv/bin/python -m evaluation.figures

Writes results/phase3/fig_*.png:
  fig_mean_wait.png    mean wait by policy, one panel per scenario (bar: mean of the seeds;
                       dots: each seed)
  fig_class_waits.png  mean wait by size class and policy, one row per scenario
  fig_x_days.png       X: mean wait of the jobs submitted each day, by policy
  fig_x_states.png     X: the adaptive policies' state over the submission week
  fig_y_bursts.png     Y: mean wait of background and burst jobs, by policy
Colors: the validated categorical slots 1-5 (dataviz reference palette); the same numbers
appear as tables in REPORT.md, since three slots are below 3:1 contrast on the surface.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "phase3"
EVAL = ROOT / "config" / "evaluation.yaml"
POLICIES = ["C0", "B1", "A1", "A2", "A3"]
COLOR = {"C0": "#2a78d6", "B1": "#eb6834", "A1": "#1baf7a", "A2": "#eda100", "A3": "#e87ba4"}
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SCEN = {"N": "N: ordinary week", "X": "X: a different pattern each day", "Y": "Y: overlapping bursts"}
X_DAYS = ["natural", "small-short\nflood", "large-long\nheavy", "one-account\nburst",
          "memory-heavy\nmedium", "natural", "alternating\nbursts"]
STATES = ["mix", "small", "medium", "large"]


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def figure(nrows: int, ncols: int, w: float, h: float):
    fig, axes = plt.subplots(nrows, ncols, figsize=(w, h), squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for ax in axes.flat:
        style(ax)
    return fig, axes


def bars(ax, values: pd.Series, seeds: pd.DataFrame | None = None, fmt: str = "{:.0f}") -> None:
    x = np.arange(len(POLICIES))
    ax.bar(x, [values.get(p, np.nan) for p in POLICIES], width=0.72,
           color=[COLOR[p] for p in POLICIES], edgecolor=SURFACE, linewidth=2)
    if seeds is not None:
        for i, p in enumerate(POLICIES):
            ys = seeds.get(p, pd.Series(dtype=float)).dropna()
            ax.scatter([i] * len(ys), ys, s=14, color=INK, zorder=3, linewidths=0)
    # the bar's value sits under its policy name, clear of the seed dots
    labels = [p if np.isnan(values.get(p, np.nan)) else f"{p}\n{fmt.format(values[p])}"
              for p in POLICIES]
    ax.set_xticks(x, labels)
    ax.set_xlim(-0.6, len(POLICIES) - 0.4)


def matrix(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["role"] == "matrix") & df["ok"].astype(bool) & df["policy"].isin(POLICIES)]


def fig_mean_wait(m: pd.DataFrame) -> None:
    fig, axes = figure(1, 3, 12, 3.8)
    for ax, s in zip(axes[0], SCEN):
        d = m[m["scenario"] == s]
        piv = d.pivot(index="workload", columns="policy", values="wait_mean_min")
        bars(ax, piv.mean(), piv)
        ax.set_title(SCEN[s], fontsize=10, color=INK, loc="left")
        ax.set_ylabel("Mean wait (min)" if s == "N" else "", fontsize=9, color=INK2)
    fig.suptitle("Mean wait by policy (bar: mean of the seeds; dots: each seed)",
                 fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_mean_wait.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_class_waits(m: pd.DataFrame) -> None:
    fig, axes = figure(3, 3, 12, 9)
    for r, s in enumerate(SCEN):
        d = m[m["scenario"] == s]
        for c, q in enumerate(["small", "medium", "large"]):
            ax = axes[r][c]
            piv = d.pivot(index="workload", columns="policy", values=f"wait_mean_min_{q}") / 60
            bars(ax, piv.mean(), piv, fmt="{:.1f}")
            ax.set_title(f"{s}: {q} jobs", fontsize=10, color=INK, loc="left")
            if c == 0:
                ax.set_ylabel("Mean wait (h)", fontsize=9, color=INK2)
    fig.suptitle("Mean wait by size class (bar: mean of the seeds; dots: each seed)",
                 fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_class_waits.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_x_days(m: pd.DataFrame) -> None:
    d = m[m["scenario"] == "X"]
    cols = [f"wait_mean_min_day{i}" for i in range(7)]
    fig, axes = figure(1, 1, 10, 4.2)
    ax = axes[0][0]
    x = np.arange(7)
    for p in POLICIES:
        v = d[d["policy"] == p][cols].mean() / 60
        ax.plot(x, v.to_numpy(), color=COLOR[p], linewidth=2, marker="o", markersize=5,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=p)
    ax.set_xticks(x, [f"day {i}\n{n}" for i, n in enumerate(X_DAYS)], fontsize=8)
    ax.set_xlim(-0.3, 6.3)
    ax.set_ylabel("Mean wait (h)", fontsize=9, color=INK2)
    # five series: a legend (end labels collide), in the empty top-right corner
    ax.legend(frameon=False, fontsize=9, ncol=5, loc="upper right", labelcolor=INK2)
    ax.set_title("X: mean wait of the jobs submitted each day (mean of the seeds)",
                 fontsize=11, color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_x_days.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_x_states(cfg: dict) -> None:
    seeds = [w for w in cfg["workloads"] if w.startswith("X-")]
    adaptive = [p for p in POLICIES if p.startswith("A")]
    fig, axes = figure(len(adaptive), len(seeds), 13, 6.5)
    for c, w in enumerate(seeds):
        for r, p in enumerate(adaptive):
            ax = axes[r][c]
            log = ROOT / cfg["run_root"] / w / p / "policy" / "controller.jsonl"
            if log.exists():
                recs = [json.loads(line) for line in log.read_text().splitlines()]
                t0 = recs[0]["t_sim"]
                t = np.array([(x["t_sim"] - t0) / 86400 for x in recs])
                y = np.array([STATES.index(x["state"]) for x in recs])
                keep = t <= 7
                ax.step(t[keep], y[keep], where="post", color=COLOR[p], linewidth=1.5)
            for dday in range(1, 7):
                ax.axvline(dday, color=GRID, linewidth=0.8)
            ax.set_yticks(range(4), STATES)
            ax.set_xlim(0, 7)
            ax.set_ylim(-0.4, 3.4)
            ax.yaxis.grid(False)
            ax.set_xticks(np.arange(7) + 0.5, [str(i) for i in range(7)])
            if r == 0:
                ax.set_title(w, fontsize=10, color=INK, loc="left")
            if c == 0:
                ax.set_ylabel(p, fontsize=10, color=INK, rotation=0, ha="right", va="center")
            if r == len(adaptive) - 1:
                ax.set_xlabel("day of the week (0 natural, 1 flood, 2 large-long, 3 one-account, "
                              "4 memory-heavy medium, 5 natural, 6 alternating)"
                              if c == 1 else "", fontsize=8, color=INK2)
    fig.suptitle("X: state of each adaptive policy over the submission week",
                 fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_x_states.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_y_bursts(m: pd.DataFrame) -> None:
    d = m[m["scenario"] == "Y"]
    fig, axes = figure(1, 2, 10, 3.8)
    for ax, (col, name) in zip(axes[0], [("wait_mean_min_base", "background jobs"),
                                         ("wait_mean_min_burst", "burst jobs")]):
        piv = d.pivot(index="workload", columns="policy", values=col)
        bars(ax, piv.mean(), piv)
        ax.set_title(f"Y: {name}", fontsize=10, color=INK, loc="left")
        ax.set_ylabel("Mean wait (min)" if col.endswith("base") else "", fontsize=9, color=INK2)
    ymax = max(a.get_ylim()[1] for a in axes[0])
    for a in axes[0]:
        a.set_ylim(0, ymax)
    fig.suptitle("Y: mean wait of background and burst jobs (bar: mean of the seeds; dots: each seed)",
                 fontsize=11, color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_y_bursts.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    cfg = yaml.safe_load(EVAL.read_text())
    m = matrix(pd.read_csv(OUT / "metrics.csv"))
    fig_mean_wait(m)
    fig_class_waits(m)
    fig_x_days(m)
    fig_x_states(cfg)
    fig_y_bursts(m)
    print("figures ->", sorted(p.name for p in OUT.glob("fig_*.png")))


if __name__ == "__main__":
    main()
