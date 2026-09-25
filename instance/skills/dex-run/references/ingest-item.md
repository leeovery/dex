# Per-item procedure

One item = corpus item → enrich → media → harvest → lens verdict → digest
→ place → wiki → receipt, and a drop at the lens verdict ends it there.
Never skip steps; never invent inputs. All commands run from the instance
root. Code writes frontmatter and state; you write prose and judgment.
Every judgment below reads the item through this instance's lens, the
owner's `lens.md` at the instance root, taken whole as the contract says.

## 1. Corpus item (mechanical: the verb writes it)

Every capture becomes a corpus item, whatever its subject, because the
owner sharing it into this instance is the curation. Nothing is judged at
the door: whether the item holds anything for this instance is the lens
verdict's question (step 5), asked once its content has landed.

```
bin/dex enrich item new inbox/<capture>.md --shared-by <owner> [--slug <slug>]
```

Corpus items are created by `item new`, never freehand — code computes the
id (both id paths: URL hash, or the `media/<id>/` directory `dex inbox`
fixed) and writes the frontmatter, so a malformed item cannot exist. The
slug derives from the capture at creation time (note text first, then the
URL's path tail); pass `--slug` only when the capture itself already gives
a better name than that derivation — a note that buries the subject, a URL
tail that is an opaque id. Item creation precedes enrichment, so a page or
video title is never in hand here, and a title learned later does not
become a rename: the id is fixed at creation, and the better name belongs
in the digest and on the pages that cite the item. The body is the owner's note **verbatim
and stays that way** — your interpretive context (what a linked video is,
how a thread relates) belongs in the digest, never the item body; thread
context lives in the enrichment via walk-up. After creation, exactly two
frontmatter fields ever change (`status`, `enrichment:`), both derived by
the engine: the listing from the enrichment directory, the status from the
ledger — `enriched` only once every unit the ownership map gives the item
has landed (or is confirmed gone or deliberately skipped), `raw` while any
is still owed. An item with no units at all derives its status from the
enrichment directory instead: `raw` while nothing is there, `enriched` once
something is. So a text-only share stays `raw` even after its digest lands,
and a media capture flips `enriched` when its description file lands, digest
or not; owed description and digest are what the run report and the digest
backstop carry, never `status`. And `enrichment: []` on an `enriched` item is
not a fault: a URL two captures share is fetched once and its output lands
under whichever item hit first, and the listing names only that item's own
directory. `bin/dex enrich status --item <id>` is where the shared unit
shows.

Then delete the capture file — the capture is preserved in git history and
its content lives on in the corpus.

## 2. Enrich (mechanical)

`bin/dex enrich run` fetches everything behind the item's URLs and files —
captions, articles, READMEs, papers, thread walk-ups, podcast audio →
transcripts (capped per run), document extraction — and its report names
what landed, what was rewritten, and what parked. Formats with no
mechanical provider appear under **Read these yourself**: those are yours
— read the document/scan with eyes and write the enrichment file yourself
(step 3). Transcription backlogs: `bin/dex enrich transcribe --limit N`;
model judgment — `--model small` for long backlogged queues, stay at the
default for dense technical audio.

## 3. Media and cognitive-floor work (judgment)

For a media capture the primary source is the media itself, so view it
and write its description: what it depicts, all legible text (OCR), and,
wherever the lens reads for them, style, palette, composition, typography
and layout. This is the media's "transcript"; make it substantive
enough to stand in for the media in text-only contexts. Write the text to
a file and let the verb place it:

```
bin/dex enrich item describe <item-id> --of <file> --file cache/description.md
```

`--of` names the file described: a path the item's `media:` states
(`media/<id>/photo.jpg`), or the bare name of a download in
`enrichment/<id>/` (`media-0.png`). Only a file the item carries is
accepted — the same reading the describe queue counts — so an extraction
asset (`<hash6>-asset-<n>.<ext>`) is refused: it owes nothing. The text is
the judgment; the `enrichment/<id>/media-<n>.md` slot, the first line
naming the file, and the item's `enrichment:`/`status` refresh are the
engine's. Never hand-write `media-<n>.md`: a hand-written description
leaves the item's derived frontmatter — its `enrichment:` listing and
`status` — stale until the next verb touches the item.

One description per media file — an item carrying three gets three, and
the verb takes the next free slot each time (slots are counted, never
paired: capture media carries no slot at all, which is why the engine's
first line is what names the file). Describing a file again rewrites its
standing description in place — how a revised reading lands. A markdown
file the item carries owes one too: nothing transcribes or extracts it,
so read it and write a summary — the read is the point, and the
description is what proves it happened. The engine counts descriptions
against media files and lists any item short of one under **Describe
these**, on the run report, `bin/dex enrich status`, and the health check,
until the count is met.

`discarded-media-<n>.md` in an enrichment directory is a retired
description: the file it described is gone (a migration found the bytes
were never media and deleted them), so it was renamed out of the family
the describe row counts, text untouched. It is a record of what a URL
once answered with, not a description of anything the item carries —
never cite it as one, and leave it alone. If media later lands in that
slot, the describe row reappears and you write a fresh description the
ordinary way.

For a cognitive job the report listed (a document no extractor reads, a
scanned PDF): read the file directly, write the enrichment as
`enrichment/<id>/<kind>-<hash6>.md` — `hash6` is the first 6 characters
of the ledger row's `hash` (grep `state/enrichment-ledger.jsonl` for the
URL) — then close the loop:

```
bin/dex enrich mark <url> done --path enrichment/<id>/<file>
```

Closing the loop is what retires the unit's earlier output: where the kind
was corrected mid-fetch (a page that turned out to be a PDF), `mark` drops
the superseded `<old-kind>-<hash6>.md` once the file you named is on disk,
so the item is left holding one enrichment for the unit, not two.

## 4. Harvest: the subject rule (judgment, engine-bounded)

Goal: the item's enrichment holds the primary artifacts of its subject
that the lens needs. At each fetched page, promote links that are
**primary artifacts of the item's subject**, choosing the ones this
instance's lens reads for: the pricing and about pages for a business
lens, the repo and docs for an engineering lens, the rendered pages
themselves for a design lens. Links that *leave* the subject
(similar-projects lists, blogrolls, footers) are never promoted, at any
depth. There is no hop-counting rule: depth is bounded mechanically by
the engine (depth 4, 12 URLs per item), and the subject rule is the
judgment inside those bounds. A shared site root or topic index is
navigation whose substance lives in its pages, so harvest is how the item
reaches them: a thin landing page whose substance sits on /pricing for a
business lens, or on /docs for an engineering one, is the subject rule
applied to the site's own pages, while a small site's root can be the
content itself, with nothing further to promote.

Promote via:

```
bin/dex enrich fetch <item-id> <url> [...]
```

— ledgered as children with provenance. **Never** add promoted URLs to the
item's `urls:` frontmatter: that list is capture provenance (what was
shared) and is immutable after ingest; what was fetched is a ledger fact.
Complete the cognitive steps for whatever the fetch report names. Then
record the pass — even when nothing was promoted ("ran and promoted
nothing" must be distinguishable from "never ran"):

```
bin/dex enrich pass <item-id> --stage harvest
```

## 5. Lens verdict (judgment: the only drop)

Once the item's content has landed (its enrichment, its media
descriptions and whatever harvest promoted), ask one question of it, read
together with the owner's note: through this instance's lens, is there
anything here? The verdict follows harvest because the substance is often
one link away, like a post whose whole point is the article it links to,
so judge the item by everything harvest brought in, never by a site root
or an index page alone.

If there is anything, however little, the item goes on to its digest: a
thin yield is a `signal: low` digest, perhaps filed in
`uncategorized-shares`, and never a drop. If there is nothing, drop the
item through the exclude verb, with a reason in lens terms that says what
the content is and why nothing in it reads through this lens, never
naming another instance it might suit. Write the record to
`cache/exclusions.json`:

```json
[{"id": "<item-id>", "reason": "<what it is, and why the lens finds nothing in it>"}]
```

```
bin/dex exclude cache/exclusions.json
```

The verb records the drop in `state/exclusions.tsv` and removes the item
with everything it left behind: its media, enrichment, digest, pass
records and ledger entries (the full behaviour is in `state-formats.md`,
this directory). A dropped item gets no digest,
placement, wiki work or receipt, and the closing report (dex-run step 6)
names it with its reason.

Nothing is dropped for its subject: a gardening site shared into a design
instance is read for its design, because the owner sharing it here has
already decided it belongs. The drop is for an item that yields nothing
through the lens, which means a share into the wrong instance or content
with nothing in it, like chatter or a one-line complaint about a tool.
The verdict judges only content that actually landed, and what could not
be read is never evidence that nothing is there, so the verdict never
reaches:

- content the engine could not reach, such as a paywall, a blocked fetch
  or a dead link, which is a fetch problem for the parking lanes and the
  heal procedure below, never a drop;
- a parked item (waiting, blocked, manual), which gets no verdict until
  its sources land, just as it gets no digest;
- an item whose every unit is dead or skipped, or whose landed content
  only points at a unit that never landed: it goes on to its digest from
  the owner's note and whatever did land (step 6), and the lens never
  drops it.

The verdict is yours alone, in a scheduled run and an attended session
alike: make it without asking the owner, and never skip an item or leave
it waiting for someone else to decide. An item that already holds a
digest is past its verdict, so a re-fetch or a requeue goes straight on
to step 6.

## 6. Digest (judgment — the values ARE the judgment; the verb writes it)

Read the enrichment fully — including viewing any media — then write the
judgment as JSON and let the engine serialize it:

```json
{"id": "<item-id>", "signal": "high", "topics": ["agent-architecture"],
 "entities": ["claude-code"],
 "facts": ["one standalone fact per fact the source yields through the lens", "..."]}
```

```
bin/dex enrich item digest --file cache/digest.json
```

`signal`, `topics`, `entities` and the facts are the judgment; the fence,
the field order and the bullets are the engine's, so a digest cannot come
out malformed. Never hand-write `state/digests/<id>.md`. The item's `date`
and `media:` are the engine's to derive from the item and its enrichment —
passing either is refused. Full shape in `state-formats.md` (this directory).

Extract the facts the source yields through this instance's lens, with
enough about its subject to say what the item is, and choose its topics
along the lens too. Judge `signal` by the same measure: a design mockup
with no product behind it is thin evidence to a business lens and rich
material to a design one.

Each fact carries concrete specifics and reads without the source in front
of you. No target count: a rich paper earns many bullets and a two-line
tweet earns two, and padding to a number invents facts. Interpretive
context lives here, not in item bodies. Topics: canonical names from
`state/taxonomy.json` when it exists; otherwise 2–5 kebab-case candidates.
Revising a digest later is the same call with a new payload — the file is
rewritten whole. The verb records the digest pass itself — the
confirmation says so — so there is no separate pass command to run here.

A parked item (waiting/blocked/manual) still exists — provenance and note
were captured at ingest — but gets no digest or wiki work until its
sources land. Don't force it; the run report's **Not finished** section
names each such item and what it is still owed ("3 of 4 units landed — 1
waiting on transcription"), and the item stays `status: raw` until the
last unit lands.

An item whose every unit is dead or skipped is not parked: a terminal
unit that landed no content owes nothing, so the item owes only its
description and digest, and both come from the owner's note. The run
report says so ("dead or ruled out" covers the mixed case), and the
report and the digest backstop name the item until its digest pass is
recorded; recording that pass is what clears it.

## 7. Place (judgment — the values ARE the judgment; the verb writes it)

Placement is stated as JSON and applied by the engine — never by editing
`state/taxonomy.json` or `state/entity-members.json` by hand:

```json
{"place": [{"id": "<item-id>", "topics": ["agent-architecture"],
            "entities": ["claude-code"]}]}
```

```
bin/dex enrich place --file cache/placement.json
```

The judgment is unchanged — what to place where, and when a topic exists
at all; the fence between you and the engine moved, exactly as it did
for digests. On the first placement pass of a fresh instance the verb
creates the file: define `uncategorized-shares` and any first topics in
the same payload's `topics` section and place the item — early items may
land in `uncategorized-shares` until pages are justified. A corpus with
no taxonomy is the state lint reads as BROKEN MID-INGEST and fails on,
so the file exists from the first placed item onward.

Topics break along the lens: from the same shares, a design instance
grows topics like typography and landing-page layout, where a marketing
instance files them under social proof and sales copy.

Create a new topic only once several items justify a page; the
several-items rule governs page creation, never taxonomy existence. When
you do create one, sweep the existing digests (`state/digests/`) for
items that belong to it — including `uncategorized-shares` — and move
them in with `place` plus `unplace` in the one payload: a new topic
usually reveals items that were previously overlooked or coarsely filed.
Full payload shape — definitions, moves, drops, and what the verb
refuses — in `state-formats.md` (this directory).

## 8. Wiki (judgment — synthesis is the point)

Write every page from the lens's angle, so that it says what this
instance reads its items for: what a design does, how a business makes
money, what a piece of copy achieves.

Splice cited sentence(s) into affected pages — rewrite-not-append when
"current state" changes. Pages citing a media item should usually embed it
(relative image links). `wiki/index.md` needs nothing — it is rendered by
the engine, and step 7's `place` already recompiled it. Append a
`wiki/log.md` line. Then `bin/dex enrich pass <item-id> --stage wiki`.

Citation rules:

- **Citations are full item ids, always** — backticked
  `YYYY-MM-DD-<slug>-<shortid>`, everywhere, the index included. A bare
  backticked shortid is a malformed citation and lint flags it as such.
- **Re-fetched item (same id)** — a rerun or a re-enrichment: grep the
  pages citing that id and *revisit those sentences* — rewrite in place
  where the content changed. Never splice additions for an already-cited
  id; that is how reruns breed restated facts.
- **Same fact from a new source** — add the citation to the existing
  sentence; don't write a new sentence.

## 9. Receipt (mechanical rendering)

Render the item's receipt through the surface — never hand-draw it. Write
`cache/receipt.json`:

```json
{"surface": "ingest-receipt",
 "payload": {"item": "<id>", "title": "...", "fetched": 3, "parked": 1,
             "signal": "high", "topics": ["..."], "pages": ["..."],
             "notes": ["..."]}}
```

then `bin/dex render --file cache/receipt.json`. In an attended session,
emit the rendered receipt verbatim — it is the owner's confirmation. In
a scheduled run the render is the item's record in the transcript; the
closing report (dex-run step 6) carries the item to the owner in its own
shape.

## Healing (manual entries — judgment, closed through the verb)

A `manual` entry parks for a stated reason (a paywall, a thin extraction,
a 402, five failed attempts). Where judgment can rescue it — you can fetch
the page with your own tools, read the content, transcribe the source —
write the enrichment file beside the mechanical outputs, and **always end
by writing the ledger through the sanctioned verb**:

```
bin/dex enrich mark <url> done --path enrichment/<id>/<file>
bin/dex enrich mark <url> skipped --reason "<why it stays unfetched>"
```

A **Needs you** row tagged `resting` is not a manual park and owes no
heal: `media_fetch: none` parks it, and the engine fetches it again once
the owner turns media back on. Leave it as it stands.

A row tagged `no provider will appear — you read this one` is the
cognitive floor: the capability it waits on has no mechanical provider
for that format, so you are the provider. Read the source, write the
enrichment file, and close it with `mark done --path` exactly as above.
Nothing else will ever move it — the run report lists the same units
under **Read these yourself**.

The ledger must match reality when the session ends — a hand-heal that
skips `mark` recreates the incident this pipeline was rebuilt to end.
Never hand-append to any `state/*.jsonl`; the verbs are the only writers.
