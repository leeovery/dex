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
   - **Ledger items with no corpus file** — one row per item and cause;
     the row's parenthesis says which of three, and they want different
     things. **Excluded on record** — ruled out on purpose: re-run
     `bin/dex exclude` with that id to purge the leftover entries (the
     recorded exclusion makes a re-run idempotent); where the row also
     names a sharer, the lines survived because a live item still claims
     the work — leave those. Instances purged before exclude swept the
     ledger carry this as historical residue: clearing it is a one-off,
     not a per-check chore. **Renamed — `<id>` lists this work** —
     history, not a repair: a ledger line's `item` is the attribution as
     of the day it was written, no line is ever rewritten, and every
     reader and every write already resolves the work to the item whose
     corpus file claims it, so the run, the status view, the digest and
     the item's own frontmatter all say the live id. The count only ever
     grows as items are renamed; if the same rename ALSO shows a misfiled
     output below, that half is real — see that entry. **No exclusions
     record, no live claimant** — an owner decision on whether the item
     comes back: restore the corpus file from git history, or rule it out
     through `bin/dex exclude`. Never edit state files by hand.
   - **Done entries whose output file is gone from disk** — the ledger
     claims work whose product is missing. Re-fetch the unit — `bin/dex
     enrich fetch <item> <url>` requeues a unit the item already owns —
     or, where you rescue the content by hand, write the enrichment file
     and close it with `bin/dex enrich mark <url> done --path <file>`.
   - **Done entries whose output sits under another item's directory** —
     the third referential row, and not the one above: nothing is missing
     here, the output is simply filed where its own item cannot see it. An
     item's `enrichment:` listing is the markdown in `enrichment/<item>/`,
     so that item shows an empty listing while the unit is `done` and never
     drainable again. It is what a rename done in steps leaves when it is
     interrupted between the corpus file and the enrichment directory — the
     row names the LIVE item, the one that cannot see its own output, and a
     rename carried all the way through shows here not at all. The repair
     is judgment, and it is one of two:
     finish the rename — move `enrichment/<old-id>/` onto the live id, and
     the listing and status follow at the next run — or, where the old
     directory is not this item's work to take, re-fetch the unit and let
     the drain file it. Moving files is not the engine's business, which is
     why this is a finding and not a repair the run makes.
   - **Malformed digests** — the message names what broke (no frontmatter
     fence, a missing or bogus field, an `id` disagreeing with its
     filename). Every one of these predates the digest verb or was
     hand-edited since, so repair by rewriting the file through the verb:
     read the facts it already states, write them as the payload in
     `.claude/skills/dex-run/references/state-formats.md`, and run
     `bin/dex enrich item digest --file cache/digest.json`. The wiki layer
     reads these files, so nothing downstream is trustworthy until they
     conform.
   - **Broken wikilinks** — typo → correct it; genuinely missing target →
     create the page if its members justify one, otherwise de-link.
   - **Bad citations** (id not in corpus) — find the right id or remove
     the claim.
   - **Shortid-shaped citations** (backticked 6-hex, index included) —
     citations are full item ids, always: resolve each to its full id or
     remove the claim.
   - **Items no page cites and the taxonomy does not record** — read the
     digest: cite it on
     the best existing page, or ledger it into `uncategorized-shares` in
     `state/taxonomy.json` if genuinely low-signal.
   - **Items a page cites but no taxonomy topic records** — the other face
     of the coverage invariant: a citation is not a placement. Read the
     digest and append the id to each matching topic's `items` in
     `state/taxonomy.json`, or ledger it into `uncategorized-shares` if
     genuinely low-signal.
   - **Pages missing from index** / **ghost index entries** — regenerate
     the affected `wiki/index.md` entries.
   - **Stale pages** (members newer than the page) — fold the newer items
     in via rewrite-not-append; if the new material supersedes old claims,
     move them to the history section marked superseded, never silently
     delete.
   - **Possible restated facts** — read each flagged pair: same fact →
     merge into one sentence carrying both citations; genuinely distinct →
     leave them (the flag is a question, not a verdict).
   - **Harvest these (no pass on record)** — the item's pages were fetched
     but no harvest pass was ever recorded: "never ran" is the standing
     state, not "ran and promoted nothing". Run the harvest judgment now
     under the current subject rule (dex-run's `references/ingest-item.md`),
     then `bin/dex enrich pass <item> --stage harvest` — the pass is
     recorded even when nothing was promoted, and recording it is what
     keeps this row empty.
   - **Harvest passes under old rules** — re-run the harvest judgment for
     those items under the current subject rule (dex-run's
     `references/ingest-item.md`), then `bin/dex enrich pass ... --stage
     harvest`.
   - **Read these yourself** (the engine cannot do them) — complete them
     per the same reference (media described with eyes, `enrich mark`
     closing each). **Waiting on a capability** is the engine's cohort,
     not yours: those units drain when a provider appears (transcription
     backlogs: `bin/dex enrich transcribe --limit N`), and the ones that
     resolve to you are already listed under **read these yourself**.
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
     is the signal. Every promotion goes through `enrich fetch`, so this
     reading covers refusals whoever typed them — a link you promoted on
     the subject rule and a URL the owner named by hand say the same thing
     about the bounds. What leaves the reading is an **override**: a
     refusal answered with `--force` was a decision taken, not drift, and
     the count already excludes it. The listing is newest
     first, like the thread markers above it: the rows the report names are
     the most recent fires, and the older ones stand behind the count.
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
