---
name: dex-lint
description: Health-check and repair this dex instance. Use when the owner asks for a health check, a lint, to "look it over", or periodically after a run of ingests.
---

# Health check (lint)

Mechanical checks come from `bin/dex lint`; the repairs are judgment. When
this runs inside a dex-run session, steps 1–3 already happened — start at
step 4.

1. `git pull` — captures and other machines' commits arrive first.
2. `bin/dex sync` — pin bump, migrations, machinery refresh. Review the
   sync report (migration `skipped`/`anomalies` are repaired with judgment)
   and commit the refreshed files and pin.
3. `bin/dex inbox` — reconcile staged captures. It needs GitHub auth (gh
   logged in, or GITHUB_TOKEN); if it reports missing auth or any FAIL
   line, stop and fix that with the owner before continuing. Follow its
   commit-and-push-now guidance when it materialized anything. If it
   reports orphaned assets, re-run after a minute before raising them with
   the owner (a capture may still be in flight).
4. `bin/dex lint --write` from the instance root — the mechanical report.
   `--write` reconciles derived wiki frontmatter (`items:` counts, missing
   `generated:` dates) mechanically; commit what it changed. Everything
   else on the report is yours to repair.
5. Fix, in order (state before wiki — a broken ledger blocks the verbs the
   other repairs need):
   - **Ledger schema failure** — repair before anything else touches
     state; the message names the file, line, and likely missing migration
     (usually: run `bin/dex sync`).
   - **Quarantine not empty** (`state/enrichment-ledger.unmigrated.jsonl`)
     — review each line, re-add what should live via
     `bin/dex enrich mark <url> <status> --reason ...`, or accept the
     loss; then empty the file.
   - **Broken wikilinks** — typo → correct it; genuinely missing target →
     create the page if its members justify one, otherwise de-link.
   - **Bad citations** (id not in corpus) — find the right id or remove
     the claim.
   - **Shortid-shaped citations** (backticked 6-hex, index included) —
     citations are full item ids, always: resolve each to its full id or
     remove the claim.
   - **Orphans** (uncited, unledgered items) — read the digest: cite it on
     the best existing page, or ledger it into `uncategorized-shares` in
     `state/taxonomy.json` if genuinely low-signal.
   - **Index drift** — regenerate the affected `wiki/index.md` entries.
   - **Stale pages** (members newer than the page) — fold the newer items
     in via rewrite-not-append; if the new material supersedes old claims,
     move them to the history section marked superseded, never silently
     delete.
   - **Possible restated facts** — read each flagged pair: same fact →
     merge into one sentence carrying both citations; genuinely distinct →
     leave them (the flag is a question, not a verdict).
   - **Harvest passes under old rules** — re-run the harvest judgment for
     those items under the current subject rule (dex-run's
     `references/ingest-item.md`), then `bin/dex enrich pass ... --stage
     harvest`.
   - **Cognitive jobs / waiting cohorts** — complete them per the same
     reference (media described with eyes, `enrich mark` closing each).
   - **Enrichment newer than digest** — an interrupted session: finish
     digest → place → wiki for each listed item.
6. Judgment sweep over every page you touched: cross-page contradictions
   (surface with dates), self-citation, scope creep.
7. Coarse-topic check: when a topic has grown unwieldy or a coherent
   sub-cluster has formed inside it, split it — create the finer-grained
   topic(s), reassign members from their digests, rewrite the affected pages
   with cross-links, and update the index. The taxonomy is flat; a "subtopic"
   is just a sharper topic.
8. Re-apply `wiki/pins.md` (the owner's hand-corrections) after any regeneration —
   pins always win.
9. Append a `wiki/log.md` line; commit and push. Report findings and fixes to
   the owner in plain words — no jargon.
