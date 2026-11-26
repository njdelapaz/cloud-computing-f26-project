"""Checks that the ver_0.3 layout and ignore rules match PLAN.md."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_folders_exist():
    for d in ["sim", "workloads", "config", "config/priority", "results", "tests"]:
        assert (ROOT / d).is_dir(), d


def test_generated_paths_are_ignored():
    paths = [
        "dataset/01.tsv",
        ".venv/x",
        "third_party/x",
        "workloads/cache/x.parquet",
        "workloads/traces/N-s1/events.txt",
        "results/runs/run1/slurm_acct.out",
        "results/build/image_build.log",
    ]
    out = subprocess.run(
        ["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True
    )
    ignored = set(out.stdout.split())
    assert ignored == set(paths), set(paths) - ignored


def test_summaries_are_not_ignored():
    out = subprocess.run(
        ["git", "check-ignore", "results/profile/weekly.csv"], cwd=ROOT, capture_output=True
    )
    assert out.returncode == 1
