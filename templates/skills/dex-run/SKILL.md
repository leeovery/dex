---
name: dex-run
description: Operate this dex instance unattended — for schedulers (Claude Cowork, cron) or "run the instance". First run sets up the schedule with the owner; every run pulls, ingests, health-checks when due, pushes, reports.
---

# Scheduled run

One run leaves the instance clean: everything ingested and pushed, or a loud
report of why not.

## First run (interactive, no schedule established yet)

Ask the owner how often this instance should run, with a recommendation:
every 2 hours for an instance receiving captures daily; once a day for a
quiet one. Then establish that schedule in the host's scheduler — if the
host can schedule recurring runs (Claude Cowork can), create the recurring
run of this skill yourself; otherwise give the owner the exact schedule to
create. Then do a run.

## Every run

1. **Guard.** If the working tree is dirty before you start, stop and
   report — a previous run may not have finished. Do not build on its state.
2. **Pull.** `git pull`.
3. **Ingest.** Process the inbox per the dex-ingest skill
   (`.claude/skills/dex-ingest`) — every capture, end to end.
4. **Health check.** If `wiki/log.md` shows no health check (a `| lint`
   entry) in the past 7 days, run the dex-lint skill
   (`.claude/skills/dex-lint`) — including when the inbox was empty.
5. **Verify.** Working tree clean, everything committed and pushed.
6. **Report.** One short summary: what was ingested, what the health check
   found, or "nothing to do". Report any failure — auth, network, a command —
   loudly. Never work around a failure by hand; never leave work half-done
   silently. A scheduled run never asks the owner questions — it acts or it
   reports.
