# Repairs

Fix, in order (state before wiki — a broken ledger blocks the verbs the
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
