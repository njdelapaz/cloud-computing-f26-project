#!/usr/bin/env python3
"""Write config/smoke/contended.events, the A1 gate's contended trace for the smoke cluster.

A 12-core job from user1 (account1) fills the 3-node, 12-core smoke cluster for 30 minutes.
Meanwhile 40 jobs from the four smoke users (two accounts) queue behind it, with 1-12 cores
and 1-10 minute runtimes. Once the filler ends, fair-share, age and job size decide which
queued job goes next, so the trace tests ordering from early on. It is small enough that
repeated runs agree to within the simulator's 1 s clock jitter, which the one-day probe
trace does not (see results/a1/summary.md).

Usage: python3 -I scripts/make_contended_events.py [--check]
  --check  exit 1 if the committed file differs from what this script writes
"""
import random
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "config" / "smoke" / "contended.events"
USERS = [("user1", "account1"), ("user2", "account1"), ("user3", "account2"), ("user4", "account2")]


def render(seed=7):
    rng = random.Random(seed)
    lines = ["-dt 0 -e submit_batch_job | -J jobid_1001 -sim-walltime 1800 --uid=user1 -t 00:40:00 -n 12 "
             "--mem-per-cpu=1000 -A account1 -p normal -q normal pseudo.job"]
    t = 10
    for i in range(1002, 1042):
        user, account = rng.choice(USERS)
        cores = rng.choice([1, 1, 2, 2, 3, 4, 4, 6, 8, 12])
        runtime = rng.randrange(60, 601)
        limit_min = runtime // 60 + 2
        lines.append(f"-dt {t} -e submit_batch_job | -J jobid_{i} -sim-walltime {runtime} --uid={user} "
                     f"-t 00:{limit_min:02d}:00 -n {cores} --mem-per-cpu=1000 -A {account} -p normal -q normal pseudo.job")
        t += rng.randrange(3, 15)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv[1:]:
        ok = OUT.exists() and OUT.read_text() == text
        print(f"{'OK  ' if ok else 'DIFF'}  {OUT.name} matches make_contended_events.py")
        sys.exit(0 if ok else 1)
    OUT.write_text(text)
    print(f"wrote {OUT} ({text.count(chr(10))} jobs)")
