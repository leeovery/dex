---
name: dex-run
description: Operate this dex instance — sync, process the inbox, drain the pipeline, health-check, push. Use for schedulers (a desktop scheduled task, cron), "run the instance", "process now", or "process the inbox".
---

# Run

One run leaves the instance clean: everything processable processed and
pushed, or a loud report of why not. The pipeline's report — not the
inbox — is the work list: `enrich run` names every item with new or
changed content, and this session completes the cognitive steps for all
of them.

Detail lives in `references/` in this skill's directory. Read each
reference at the step that names it, not before — a reference's
instructions apply when its step is reached.

## Every run

1. **Prepare.** Read `references/preparation.md` now and perform its five
   steps: anchor → sync → guard → pull → inbox. The invariants: stay
   inside this instance's root for the whole run and never enter another
   dex instance; `bin/dex sync` runs before anything else touches state;
   after sync's commit, a still-dirty tree means a previous run died
   mid-work — stop and report.

2. **Process.** Read `references/processing.md` now and perform its three
   steps: items created from every in-scope capture, then `bin/dex enrich
   run` with its report completed section by section, then the `bin/dex
   enrich status` backstop. It dispatches the per-item cognitive work —
   scope check through receipt — to `references/ingest-item.md`.

3. **Health check.** Log lines are `## [YYYY-MM-DD] <op> | <title>`, so a
   health check reads `## [<date>] lint | …`. If `wiki/log.md` holds no such
   line dated within the past 7 days, run the dex-lint skill
   (`.claude/skills/dex-lint`) — including when the inbox was empty.

4. **Verify.** Working tree clean, everything committed and pushed.

5. **Report.** One short summary: what was ingested, what was parked and
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
  (details in `references/ingest-item.md`).
- Cap-fired events (depth/URL caps) are internal — they live in the
  ledger for the health check. An `enrich fetch` refusal is already
  answered on the engine's own report; the summaries and receipts you
  write never restate cap fires.

## State writes

Where a state file has a verb, the verb writes it and you never do:
corpus items come from `enrich item new`, digests from `enrich item digest
--file cache/digest.json` (which records the digest pass itself), ledger
lines from `enrich mark` and the run itself, harvest and wiki stage
records from `enrich pass`. You supply the judgment as JSON
or arguments; the engine decides the shape, so a malformed file cannot be
written. Same motion as rendering, below. `state/taxonomy.json` and
`state/entity-members.json` are the ones you still write directly.

## Rendering

Never hand-draw a receipt or report — there is a surface for it. Surfaces
emit markdown: headings, bullets, and identity (item ids, URLs, paths)
whole and never truncated.
Write the payload JSON to `cache/`, run
`bin/dex render --file cache/<name>.json` (the file is
`{"surface": "<name>", "payload": {...}}`), and emit the output verbatim.
Engine commands (`enrich run`, `enrich status`, `sync`, `lint`) already
render their own reports — reproduce those verbatim too.

## Backfills (exports in raw/)

Exports land in `raw/` and go through `bin/dex normalize`. Read
`references/backfills.md` now — where exports come from, the
converted-export contract, and the flow at scale.
