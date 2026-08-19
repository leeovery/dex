---
name: dex-run
description: Operate this dex instance unattended — for schedulers (a desktop scheduled task, cron) or "run the instance". First run sets up the schedule with the owner; every run pulls, ingests, health-checks when due, pushes, reports.
---

# Scheduled run

One run leaves the instance clean: everything ingested and pushed, or a loud
report of why not.

## First run (interactive, no schedule established yet)

Ask the owner how often this instance should run, with a recommendation:
hourly for an instance receiving captures daily; once a day for a quiet one.
Then arm the schedule: a **local scheduled task** in the Claude desktop app
(Code tab → Routines → New routine → Local), which runs this skill on the
owner's machine with their full environment. The task:

- Name `<instance>-run`; the chosen frequency; instructions exactly:
  "Work in the folder <absolute path to this instance>.
  This is a scheduled, unattended run. Read .claude/skills/dex-run/SKILL.md
  and perform its Every-run procedure exactly. Never ask the owner
  questions; report what was done."
  The first line is load-bearing: task creation from a session cannot set
  the task's folder (no folder parameter), so the prompt must carry it.
- In a desktop session that can create scheduled tasks, create it yourself;
  otherwise hand the owner those exact values for the New-routine form
  (Local; worktree off; permission mode Auto).
- The created task's Folder field will show the session's folder, not the
  instance. Walk the owner through fixing it: open the task → Edit → the
  Folder field sits below the prompt → set it to the instance → save.
- Then have the owner Run now once, answering permission prompts with
  "always allow", so scheduled firings never stall.
- Local tasks fire only while the app is open and the machine awake; a
  missed schedule catches up once on wake. Captures wait in the inbox
  meanwhile — nothing is lost.

Then do a run now, whatever the host.

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
