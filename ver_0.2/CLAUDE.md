# CLAUDE.md

## Scope: stay inside `ver_0.2/`
- Read, search, write and run things only inside this folder. Never open, list or search files in the parent repo or in sibling folders such as `ver_0.1/`.
- Don't reuse code, configs or results from outside this folder, even if it looks relevant. If something from outside seems useful, ask first.
- Git commands also see the parent repo, so limit them to this folder (for example `git status -- .`, `git diff -- .`, `git log -- .`). Don't run repo-wide listings like `git ls-files` from the parent.
- Don't save to Claude's auto-memory, which lives outside this folder. Put lasting project rules in this file instead.

## Project files
- `SPEC.md`: the spec (what and why). Change it only after an explicit decision.
- `SPEC.proposed.md`: the expanded draft spec, waiting to be merged into `SPEC.md`.
- `WORKFLOW.md`: the phased plan with a gate check at the end of each phase.
- `PROGRESS.md`: the progress tracker (create it in phase P0).

## Progress tracking
- Keep `PROGRESS.md` up to date: status, phase tracker, decisions log, results, change log.
- Leave out task lengths, target dates and timelines. Dated change-log entries for past commits are fine.
- Track progress in the repo file only. Don't create a separate live doc.
