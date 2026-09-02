# Repairs

Fix, in order (state before wiki — a broken ledger blocks the verbs the
other repairs need):

- **Ledger schema failure** — repair before anything else touches
  state; the advice splits by fault. A line that is valid JSON but
  violates the schema is stale vocabulary: the message names the file,
  line, and likely missing migration (usually: run `bin/dex sync`). A
  line that does not parse as JSON at all is a torn write, and takes
  the torn-line sanction below: delete exactly the named line.
- **Torn state file** — the failure row (or sync's error) names the
  offending file AND line. A torn line in
  `state/enrichment-ledger.jsonl`, `state/passes.jsonl` or
  `state/migrations.jsonl` is an interrupted write (it starts trailing,
  but later appends or a union merge can leave it mid-file), and the
  sanctioned repair is deleting exactly the line the row names: a
  half-written line is not a record, so removing it is healing, not
  hand-writing state. The sanction is that narrow — the named TORN line
  only, never an intact record and never a rewrite. A malformed
  `state/taxonomy.json` or `state/entity-members.json` takes its own
  narrow sanction: the placement verb refuses to build on a file it
  cannot read, so hand-repair the file to the shape in dex-run's
  `references/state-formats.md` — restoring a readable shape is healing,
  not hand-writing state — and every write after that goes through
  `bin/dex enrich place`.
- **Ledger items with no corpus file** — one row per item and cause;
  the row's parenthesis says which of three, and they want different
  things. **Excluded on record** — ruled out on purpose: write the id as
  an exclusions batch (`[{"id": "<item-id>", "reason": "<why>"}]` in
  `cache/exclusions.json` — full shape in dex-run's
  `references/state-formats.md`) and run `bin/dex exclude
  cache/exclusions.json` to purge the leftover entries (the recorded
  exclusion makes a re-run idempotent); where the row also
  names a sharer, the lines survived because a live item still claims
  the work — leave those. Instances purged before exclude swept the
  ledger carry this as historical residue: clearing it is a one-off,
  not a per-check chore. **Renamed — `<id>` lists this work** —
  history, not a repair: a ledger line's `item` is the attribution as
  of the day it was written, no line is ever rewritten, and every
  reader and every write already resolves the work — children, media
  and assets included, up the parent chain — to the item whose corpus
  file claims it, so the run, the status view, the digest and the
  item's own frontmatter all say the live id. The count only ever
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
  the drain file it. Finishing the rename settles everything: a digest
  left under the old id in `state/digests/` needs no move, because the
  backstop resolves it by shortid to the live item. Moving files is not
  the engine's business, which is why this is a finding and not a
  repair the run makes.
- **Malformed digests** — the message names what broke (no frontmatter
  fence, a missing or bogus field, an `id` disagreeing with its
  filename). Every one of these predates the digest verb or was
  hand-edited since, so repair by rewriting the file through the verb:
  read the facts it already states, write them as the payload in
  `.claude/skills/dex-run/references/state-formats.md`, and run
  `bin/dex enrich item digest --file cache/digest.json`. The wiki layer
  reads these files, so nothing downstream is trustworthy until they
  conform.
- **Re-emit these digests** (`media:` is not the media on disk) — an
  advisory, and it never fails the check. What a digest states and what
  the item carries — its own `media:` paths, then the media files sitting
  in `enrichment/<id>/` — are no longer the same set, in one direction or
  the other: a file that landed after the digest was written, or a path
  left standing for a file since gone. Order and list form are not drift;
  membership is. The wiki layer embeds from this listing, so a page built
  off a stale one points at nothing, or misses the capture it is about.
  Digests written before the engine derived the listing carry this as a
  standing state, and it clears exactly as a malformed digest does:
  rewrite the file through the verb — read the facts it already states,
  write them as the payload, run `bin/dex enrich item digest --file
  cache/digest.json` — and the engine re-derives `media:` itself. Never
  hand-edit the line in. One exception to reusing the facts: when the
  stated path names a file since gone and a different file now stands in
  its place, the evidence the digest was read from has changed — re-read
  the item against the file it carries now and digest it fresh, carrying
  none of the old facts forward. The path drifting is the symptom; a
  digest describing a file the item no longer holds is the defect.
- **Digests naming a canonical topic that does not record them** — an
  advisory, and it never fails the check. A digest's `topics:` is the
  classification made at digest time, in that day's vocabulary, and is
  never rewritten when the taxonomy moves on — so most disagreement is
  expected and stays silent. What surfaces is narrower: the digest names
  a topic the taxonomy DOES define, and that topic's `items` does not
  record the id. Read the digest and judge which of two it is: a
  placement miss — the reading still holds, the filing never happened —
  repaired with a `place` record through `bin/dex enrich place`; or a
  rename residue / a reading placement has since overruled — the item
  now lives under a different name, and the digest is the permanent
  record of what it was called then — which repairs nothing: leave it,
  and it stands on the report as a known reading.
- **Describe these** (media carried, no description written) — an
  advisory, and it never fails the check. The item holds more media files
  — its own `media:` paths, plus the `media-<n>.<ext>` files downloaded
  into `enrichment/<id>/` — than it holds `media-<n>.md` descriptions, and
  the row says how many of how many are written. Extraction assets
  (`<hash6>-asset-<n>.<ext>`) are not counted: they are figures out of a
  document already extracted, and owe nothing. The repair is the work
  itself — view or read the media and write the missing descriptions per
  dex-run's `references/ingest-item.md` (step 4), one per file, in the
  next free slot. Nothing mechanical clears this row; a bulk-ingested
  backlog stands here until a session looks at it.
- **Broken wikilinks** — typo → correct it; genuinely missing target →
  create the page if its members justify one, otherwise de-link.
- **Bad citations** (id not in corpus) — find the right id or remove
  the claim.
- **Shortid-shaped citations** (backticked 6-hex, index included) —
  citations are full item ids, always: resolve each to its full id or
  remove the claim.
- **Items no page cites and the taxonomy does not record** — read the
  digest: cite it on the best existing page, or ledger it into
  `uncategorized-shares` if genuinely low-signal — a `place` record
  through `bin/dex enrich place` (payload shape in dex-run's
  `references/state-formats.md`), never a hand-edit of
  `state/taxonomy.json`. The row exempts an
  item still parked short of its digest — units non-terminal, no
  digest owed yet: coverage is owed once the item is digestible, not
  while it waits.
- **Items a page cites but no taxonomy topic records** — the other face
  of the coverage invariant: a citation is not a placement. Read the
  digest and place the id into each matching topic through `bin/dex
  enrich place`, or ledger it into `uncategorized-shares` if genuinely
  low-signal.
- **Ghost members** (a topic or an entity lists an id no live corpus
  item answers) — exclusion's leftover: the purge removes the corpus
  file, enrichment, digest and ledger entries, and leaves the id
  wherever `state/taxonomy.json` or `state/entity-members.json` lists
  it, because placement ids are never checked against the corpus. The
  row names the list; remove the id with an `unplace` payload through
  `bin/dex enrich place` (shape in dex-run's
  `references/state-formats.md`) — for an excluded item, that is the
  whole repair; `lint --write` reconciles the page counts after.
- **Map artifacts stale or missing** — `state/map.json` or `wiki/index.md`
  is not what a recompile renders (a wiki edit since the last compile
  does this legitimately). The whole repair is `bin/dex map`; never
  hand-edit the index — it is rendered, and a hand edit is exactly what
  this row flags at the next check.
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

A repair can also surface a defect in the ENGINE itself: the lint
report states something false, a verb writes the wrong thing, a
documented behaviour does not happen. File it with `bin/dex issue` per
the "Engine defects" rubric in
`.claude/skills/dex-run/references/processing.md` — engine misbehaviour
only, mechanics only (never slugs, ids, URLs or paths; the verb refuses
leaks and names the field to abstract) — then carry on with the check:
filing is a side errand, never a stop.
