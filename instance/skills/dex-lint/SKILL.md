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
   - **Ledger entries naming items with no corpus file** / **done entries
     whose output file is gone** — `bin/dex exclude` purges an item's
     ledger entries with the item, so these are anomalies now, not
     leftovers by design: either a purge that died partway, or something
     deleted by hand. The finding says which — an item with an
     `exclusions.tsv` record was ruled out on purpose and its entries
     should follow it out; one without needs an owner decision on whether
     the item comes back. Either way close it through the verbs, never by
     editing state files. Instances purged before exclude swept the ledger
     carry this as historical residue: clearing it is a one-off, not a
     per-check chore.
   - **Malformed digests** — the message names what broke (no frontmatter
     fence, a missing or bogus field, an `id` disagreeing with its
     filename). Repair against `.claude/skills/dex-run/references/state-formats.md`;
     the wiki layer reads these files, so nothing downstream is trustworthy
     until they conform.
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
6. Read the standing signals — they are counts, not a queue that drains.
   Nothing clears either one, so the same numbers stand at the next check
   and a rising count is the only news in them. Neither is a chore to work
   through, and neither fails the check.
   - **Stored threads recorded incomplete** — the walk-up stopped at a
     parent it could not fetch (`chain_incomplete`), or at the driver's
     100-hop bound (`thread_cap_hit`). The bound is a sanity stop against a
     cycle or a self-referencing parent, not an editorial one: 30-post
     threads walk clean, so a cap hit is about the chain being pathological,
     not about a root left behind. A dead parent stays dead, so these never
     go away; the listing is newest first, and the ones that matter are the
     items you are about to write about. When you use one, make the gap
     explicit: a digest bullet saying what is missing, and no page sentence
     that reads the stored chain as the whole thread.
   - **Re-entry cap fires** — a tuning signal about the depth-4 / 12-URL
     bounds. Read the refused URLs: primary artifacts of the item's
     subject being turned away means the bounds are too tight for this
     corpus (raise it with the owner — the caps live in the engine);
     blogrolls and footers being turned away means harvest is promoting
     what the subject rule would not, and the fix is the judgment, not the
     bound. Concentrated in one or two items is normal; spread across many
     is the signal. A URL you asked for yourself with `enrich fetch` and
     were refused is no part of this reading — it comes through as a note,
     and the answer to it is `--force` or nothing.
7. Judgment sweep over every page you touched: cross-page contradictions
   (surface with dates), self-citation, scope creep.
8. Coarse-topic check: when a topic has grown unwieldy or a coherent
   sub-cluster has formed inside it, split it — create the finer-grained
   topic(s), reassign members from their digests, rewrite the affected pages
   with cross-links, and update the index. The taxonomy is flat; a "subtopic"
   is just a sharper topic.
9. Re-apply `wiki/pins.md` (the owner's hand-corrections) after any regeneration —
   pins always win.
10. Append a `wiki/log.md` line — `## [YYYY-MM-DD] lint | <what the check
    found>`. The op word is `lint` exactly: dex-run reads this line to decide
    whether a health check is due. Commit and push. Report findings and fixes
    to the owner in plain words — no jargon.
