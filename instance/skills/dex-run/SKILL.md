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
   mid-work — never build on it silently; the reference says how to
   recover.

2. **Process.** Read `references/processing.md` now and perform its three
   steps: items created from every in-scope capture, then `bin/dex enrich
   run` with its report completed section by section, then the `bin/dex
   enrich status` backstop. It dispatches the per-item cognitive work —
   scope check through receipt — to `references/ingest-item.md`.

3. **Compile the map.** Run `bin/dex map`. The placement and exclusion
   verbs recompile on their own writes, but wiki pages are edited
   directly — this closes the gap the session's page edits open, before
   the health check diffs the artifacts.

4. **Health check.** Log lines are `## [YYYY-MM-DD] <op> | <title>`, so a
   health check reads `## [<date>] lint | …`. If `wiki/log.md` holds no such
   line dated within the past 7 days, run the dex-lint skill
   (`.claude/skills/dex-lint`) — including when the inbox was empty.

5. **Verify.** Working tree clean, everything committed and pushed (a
   local-only instance, with no origin remote, commits without pushing:
   the push is skipped, not passed).

6. **Report.** Close with one report to the owner. Its content is fixed;
   its form is yours. Cover: what was ingested (full item ids), what was
   parked and why, what the health check found or why it was not due,
   any engine defect filed, any config change proposed, and the end
   state (pushed commit, or clean with nothing to push) — or "nothing to
   do", which covers every empty section in one line. Shape the rest to
   the run — prose where there is substance, bullets where there is
   little — but item ids, URLs, and paths reach the owner whole, never
   truncated. Report any failure — auth, network, a command — loudly.
   Never work around a failure by hand; never leave work half-done
   silently. A scheduled run never asks the owner questions — it acts or
   it reports.

   While the sync report carries a **Chat connection** line, close with
   it: one last footnote, in your own words, saying this dex is not
   reachable from the Claude desktop app's chat (or is not in its
   roster — whichever sync raised) and offering to connect it. On a yes,
   read the URL that line names and do the whole setup yourself; the
   owner never runs anything.

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
- An ENGINE defect you observe — output contradicting state on disk, a
  documented behaviour that did not happen, a verb writing the wrong
  thing — is filed with `bin/dex issue` per the rubric in
  `references/processing.md`, and then the run continues: filing is a
  side errand, never a stop. World and content failures (dead links,
  paywalls, blocked fetches, poor transcripts) never file — the
  classifier owns the world.

## State writes

Where a state file has a verb, the verb writes it and you never do:
corpus items come from `enrich item new`, digests from `enrich item digest
--file cache/digest.json` (which records the digest pass itself), ledger
lines from `enrich mark` and the run itself, harvest and wiki stage
records from `enrich pass`, taxonomy and entity members from `enrich
place`. You supply the judgment as JSON or arguments; the engine decides
the shape, so a malformed file cannot be written. Same motion as
rendering, below.

## Rendering

A state-bearing report — a receipt, a status view, an enrich or health
report — renders through its surface, never hand-drawn. Write the
payload JSON to `cache/`, run `bin/dex render --file cache/<name>.json`
(the file is `{"surface": "<name>", "payload": {...}}`). Engine commands
(`enrich run`, `enrich status`, `sync`, `lint`) already render their own
reports.

Verbatim is scoped to quotation: whenever a surface's or an engine
command's output is shown — to answer the owner, or because a step says
to emit it — reproduce it whole, identity (item ids, URLs, paths)
untruncated, never paraphrased into a new layout. The closing report
(step 6) is the one report that is not a surface's output: it is
composed with judgment against step 6's content list, and it summarizes
surface output rather than pasting it.

## Backfills (exports in raw/)

Exports land in `raw/` and go through `bin/dex normalize`. When exports
are waiting there, read `references/backfills.md` — where exports come
from, the converted-export contract, and the flow at scale.
