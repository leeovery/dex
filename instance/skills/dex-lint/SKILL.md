---
name: dex-lint
description: Health-check and repair this dex instance. Use when the owner asks for a health check, a lint, to "look it over", or periodically after a run of ingests.
---

# Health check (lint)

Mechanical checks come from `bin/dex lint`; the repairs are judgment.
Detail lives in `references/` in this skill's directory. Read each
reference at the step that names it, not before.

1. **Prepare.** Inside a dex-run session this already happened — go
   straight to step 2. Standalone, the instance is made current exactly
   as a run makes it current: read
   `.claude/skills/dex-run/references/preparation.md` now and perform its
   five steps (anchor → sync → guard → pull → inbox), then return here.
   Preparation only — create no items and run no `bin/dex enrich run`;
   processing is dex-run's job, and this skill's job is the check.

2. **Report.** `bin/dex lint --write` from the instance root — the
   mechanical report. `--write` reconciles derived wiki frontmatter
   (`items:` counts, missing `generated:` dates) mechanically; commit
   what it changed. Everything else on the report is yours to repair.

3. **Repair.** Read `references/repairs.md` now and fix what the report
   names, in the reference's order — state before wiki, because a broken
   ledger blocks the verbs the other repairs need. Never edit state
   files by hand.

4. **Standing signals.** Read `references/standing-signals.md` now and
   read both signals — counts to interpret, not a queue that drains, and
   neither fails the check.

5. **Judgment sweep** over every page you touched: cross-page
   contradictions (surface with dates), self-citation, scope creep.

6. **Coarse-topic check**: when a topic has grown unwieldy or a coherent
   sub-cluster has formed inside it, split it — create the finer-grained
   topic(s), reassign members from their digests, rewrite the affected
   pages with cross-links, and update the index. The taxonomy is flat; a
   "subtopic" is just a sharper topic.

7. **Re-apply `wiki/pins.md`** (the owner's hand-corrections) after any
   regeneration — pins always win.

8. **Compact.** `bin/dex enrich compact` — the ledger is append-only,
   so compact is what settles superseded lines and union merges, and
   the health check is its home: no other operating surface runs it.

9. **Log, push, report.** Append a `wiki/log.md` line — `## [YYYY-MM-DD]
   lint | <what the check found>`. The op word is `lint` exactly: dex-run
   reads this line to decide whether a health check is due. Commit and
   push (a local-only instance, with no origin remote, commits without
   pushing: the push is skipped, not passed). Report findings and fixes
   to the owner in plain words — no jargon.
