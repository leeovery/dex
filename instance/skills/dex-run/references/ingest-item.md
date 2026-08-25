# Per-item procedure

One item = scope check → corpus item → enrich → harvest → digest → place →
wiki → receipt. Never skip steps; never invent inputs. All commands run
from the instance root. Code writes frontmatter and state; you write prose
and judgment.

## 1. Scope check (fresh captures only — judgment)

Goal: only in-scope items enter the corpus. Bar: the capture's *content*
matches CLAUDE.md's In-scope list — if the capture has `media:`, view the
media first and judge what it actually shows. Boundaries:

- Attended session, unsure → ask the owner.
- Unattended run, borderline → do NOT guess: leave the capture file in
  place, skip it, and name it in the report for an attended decision.
- Rejected → delete the capture file (and its `media/<item-id>/` directory
  if one was created) and say why.
- Navigational/index URLs (site roots, topic indexes) usually fail scope:
  their substance lives in their pages, which is the harvest subject
  rule's job when the substance is wanted, and a skip when it isn't.
  Don't enrich navigation into empty items. This is judgment, not a
  blanket rule — a small site's root can be the content itself.

## 2. Corpus item (mechanical — the verb writes it)

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
is still owed. The status is what the item owes, never what its own
directory holds; the one exception is a capture that seeded nothing at all,
which stays `raw` on no units and no files because it still owes its
description and its digest. So `enrichment: []` on an `enriched` item is
not a fault: a URL two captures share is fetched once and its output lands
under whichever item hit first, and the listing names only that item's own
directory. `bin/dex enrich status --item <id>` is where the shared unit
shows.

Then delete the capture file — the capture is preserved in git history and
its content lives on in the corpus.

## 3. Enrich (mechanical)

`bin/dex enrich run` fetches everything behind the item's URLs and files —
captions, articles, READMEs, papers, thread walk-ups, podcast audio →
transcripts (capped per run), document extraction — and its report names
what landed, what was rewritten, and what parked. Formats with no
mechanical provider appear under **Read these yourself**: those are yours
— read the document/scan with eyes and write the enrichment file yourself
(step 4). Transcription backlogs: `bin/dex enrich transcribe --limit N`;
model judgment — `--model small` for long backlogged queues, stay at the
default for dense technical audio.

## 4. Media and cognitive-floor work (judgment)

For a media capture the primary source is the media itself: view it and
write `enrichment/<id>/media-0.md` — what it depicts, all legible text
(OCR), and, where relevant to this instance's domain, style, palette,
composition, typography, layout. This is the media's "transcript"; make it
substantive enough to stand in for the media in text-only contexts.

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

## 5. Harvest — the subject rule (judgment, engine-bounded)

Goal: the item's enrichment holds the primary artifacts of its subject.
At each fetched page, promote links that are **primary artifacts of the
item's subject** — the project's repo, its homepage, its docs, the paper.
Links that *leave* the subject (similar-projects lists, blogrolls,
footers) are never promoted, at any depth. There is no hop-counting rule:
depth is bounded mechanically by the engine (depth 4, 12 URLs per item),
and the subject rule is the judgment inside those bounds — a thin landing
page whose substance is on /pricing and /docs is the subject rule applied
to the site's own pages.

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

## 6. Digest (judgment — the values ARE the judgment; the verb writes it)

Read the enrichment fully — including viewing any media — then write the
judgment as JSON and let the engine serialize it:

```json
{"id": "<item-id>", "signal": "high", "topics": ["agent-architecture"],
 "entities": ["claude-code"],
 "facts": ["one standalone fact per fact the source actually yields", "..."]}
```

```
bin/dex enrich item digest --file cache/digest.json
```

`signal`, `topics`, `entities` and the facts are the judgment; the fence,
the field order and the bullets are the engine's, so a digest cannot come
out malformed. Never hand-write `state/digests/<id>.md`. The item's `date`
and `media:` are the engine's to fill from the corpus item — passing either
is refused. Full shape in `state-formats.md` (this directory).

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

An item whose every unit died is not parked: a dead unit owes nothing,
so the item owes only its description and digest, and both come from the
owner's note (plus anything that did land). The run report and the
digest backstop name it until its digest pass is recorded; recording
that pass is what clears it.

## 7. Place (judgment)

Placement always writes `state/taxonomy.json`. On the first placement
pass of a fresh instance, create the file (shape in `state-formats.md`,
this directory) and place the item in it: early items may land in
`uncategorized-shares` until pages are justified. A corpus with no
taxonomy is the state lint reads as BROKEN MID-INGEST and fails on, so
the file exists from the first placed item onward.

Taxonomy exists → append the id to each matching topic's `items`. Create a
new topic only once several items justify a page; the several-items rule
governs page creation, never taxonomy existence. When you do create one,
sweep the existing digests (`state/digests/`) for items that belong to
it — including `uncategorized-shares` — and move them in: a new topic
usually reveals items that were previously overlooked or coarsely filed.

## 8. Wiki (judgment — synthesis is the point)

Splice cited sentence(s) into affected pages — rewrite-not-append when
"current state" changes. Pages citing a media item should usually embed it
(relative image links). Update `wiki/index.md`; append a `wiki/log.md`
line. Then `bin/dex enrich pass <item-id> --stage wiki`.

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

then `bin/dex render --file cache/receipt.json` and emit the output
verbatim.

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

The ledger must match reality when the session ends — a hand-heal that
skips `mark` recreates the incident this pipeline was rebuilt to end.
Never hand-append to any `state/*.jsonl`; the verbs are the only writers.
