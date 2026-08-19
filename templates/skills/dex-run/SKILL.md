---
name: dex-run
description: One unattended operations run of this dex instance — for schedulers (Claude Cowork, cron) or "run the instance". Pull, ingest pending captures, health-check when due, push, report.
---

# Scheduled run

One run leaves the instance clean: everything ingested and pushed, or a loud
report of why not.

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
   silently.
