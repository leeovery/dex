---
name: dex-run
description: Operate this dex instance — sync, process the inbox, drain the pipeline, health-check, push. Use for schedulers (a desktop scheduled task, cron), "run the instance", "process now", or "process the inbox". First run sets up the schedule with the owner.
---

# Run

One run leaves the instance clean: everything processable processed and
pushed, or a loud report of why not. The pipeline's report — not the
inbox — is the work list: `enrich run` names every item with new or
changed content, and this session completes the cognitive steps for all
of them.

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
- **Model**: recommend the owner set the task's model to **Opus-class or
  above** — the run's core work is judgment (scope, harvest, digests, wiki
  synthesis), and a lighter model degrades it invisibly. Skills cannot pin
  a model; the scheduled task can. dex is not Claude-exclusive (any agent
  runtime that reads skills could drive it) but is untested elsewhere —
  say so rather than implying portability is verified.
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
- An owner who doesn't use the desktop app has no local task to arm. The
  trigger is theirs then — a cron or launchd entry that starts a session in
  this instance, or nothing at all, since asking for a run does the same
  work. Record what they chose and move on: an unscheduled instance is a
  supported state, not a failed setup.

Then do a run now, whatever the host.

## Every run

1. **Anchor.** Confirm the working directory is this instance's root (the
   folder containing this `.claude/`); change to it first if it isn't, and
   stay inside it for the whole run. Never enter another dex instance — if
   siblings exist nearby, ignore them; they might as well not exist.

2. **Sync first.** `bin/dex sync` — before anything else touches state,
   because sync bumps the engine pin and runs migrations, and a run on
   stale code writes stale vocabulary into state that newer code then
   rejects. Then:
   - **Review the sync report.** Any migration `skipped`/`anomalies` are
     repaired with judgment now, before other work builds on them — the
     code declined what it could not do safely and said so; do not ignore
     it. If quarantined ledger lines exist
     (`state/enrichment-ledger.unmigrated.jsonl`), run the quarantine
     review procedure in `references/state-formats.md` (exclusions
     cross-check first, `enrich mark` for keepers, then empty the file).
   - **Commit the refreshed files and the pin** — this skill owns that
     commit step; sync itself never commits. Message: `sync: engine <tag>`
     (or `sync: machinery refresh` when unpinned).

3. **Guard.** The dirty-tree check runs *after* sync's commit,
   deliberately: sync legitimately edits state (pin bump, migration
   rewrites and seeds) before any guard could pass, so a guard placed
   earlier would trip on its own machinery. After the sync commit, a
   still-dirty tree means a previous run died mid-work — stop and report;
   do not build on its state.

4. **Pull.** `git pull` — captures arrive as commits.

5. **Inbox.** `bin/dex inbox` — materializes staged binary captures. It
   needs GitHub auth (gh logged in, or GITHUB_TOKEN); if it reports
   missing auth or any FAIL line, stop and report — never work around it
   by hand-downloading. Follow its output: when it materialized anything,
   commit and push immediately, for the reason it states (the release
   assets are deleted; the repo copy is the only copy until pushed).

6. **Items.** For each capture file in `inbox/`: the scope check, then
   `bin/dex enrich item new` — the per-item reference
   (`references/ingest-item.md` in this skill's directory) prescribes
   both. Item creation is mechanical and fast; it must precede the enrich
   run so the pipeline seeds the new items' URLs.

7. **Enrich.** `bin/dex enrich run`, and **read its report — it is the
   work list**. Complete the per-item cognitive work
   (`references/ingest-item.md`) for *everything* it names: fresh
   captures, drained reruns, changed items, newly drained waiting
   cohorts, its listed cognitive jobs, and its no-source items (text or
   image captures — nothing to fetch, everything to describe and digest).
   Items you created this session always get the full per-item procedure
   whether or not the report names them. Parked entries
   (waiting/blocked/manual) each show a stated reason — manual ones are
   yours to judge, per the reference's heal procedure.

8. **Backstop.** `bin/dex enrich status` — any item listed under
   "enrichment newer than digest" is an interrupted previous session:
   complete its digest → place → wiki steps now.

9. **Health check.** If `wiki/log.md` shows no health check (a `| lint`
   entry) in the past 7 days, run the dex-lint skill
   (`.claude/skills/dex-lint`) — including when the inbox was empty.

10. **Verify.** Working tree clean, everything committed and pushed.

11. **Report.** One short summary: what was ingested, what was parked and
    why, what the health check found, or "nothing to do". Report any
    failure — auth, network, a command — loudly. Never work around a
    failure by hand; never leave work half-done silently. A scheduled run
    never asks the owner questions — it acts or it reports.

## Unattended rules

- **Never edit `state/config.json`** (or any owner-editable config) in an
  unattended run. A config change is a policy decision; the run's job is
  to surface it: put the proposed change and its rationale in the report,
  and the owner ratifies it in an attended session.
- Borderline scope calls are skipped and reported, never guessed
  (details in the reference).
- Cap-fired events (depth/URL caps) are internal — they live in the
  ledger for the health check and appear in no report you write.

## Rendering

Never hand-draw a table, receipt, or report — there is a surface for it.
Write the payload JSON to `cache/`, run
`bin/dex render --file cache/<name>.json` (the file is
`{"surface": "<name>", "payload": {...}}`), and emit the output verbatim.
Engine commands (`enrich run`, `enrich status`, `sync`, `lint`) already
render their own reports — reproduce those verbatim too.

## Backfills (exports in raw/)

Getting exports: Discord via
[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter), as
JSON — the format `bin/dex normalize` reads. Other sources: convert to that
shape, or feed items through capture instead.

`bin/dex normalize` → scope-filter pass (judgment; purge via `bin/dex
exclude <file.json>`) → `bin/dex enrich run` → the per-item work at scale.
Per-driver politeness sleeps make large cohorts slow by design, and the
pacing is automatic: fresh work drains first, then rerun cohorts
(migration reseeds and other requeues) at most 50 per run — the report
states the cohort position, and the remainder drains itself across
subsequent runs. No `--limit` babysitting needed. For 500+ items: work in
waves; write work manifests BEFORE dispatching any parallel agents; every
agent contract includes "if an input is missing, STOP — do not improvise";
verify coverage mechanically between waves.
