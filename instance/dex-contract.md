# dex instance contract (engine-synced)

This file is machinery, overwritten by `bin/dex sync` — never edit it in an
instance. Engine: github.com/leeovery/dex, run via `bin/dex <cmd>` from the
instance root.

## Operations

Four operations. Detailed procedures live in skills — load them, don't improvise:

- **capture** (`.claude/skills/dex-capture`) — a link, file, or note handed
  over in any session becomes one capture file in `inbox/`, committed and
  pushed. Capture only; it never processes. One route in.
- **run** (`.claude/skills/dex-run`) — the one route through: sync →
  migrations reviewed → pull → directives → inbox → items → `enrich run` →
  per-item cognitive work for everything its report names (harvest → lens
  verdict → digest → place → wiki) → `bin/dex map` → health check when due
  → push. Covers scheduled runs, "process now", and "process the inbox"
  alike.
- **query** (`.claude/skills/dex-query`) — answer from the wiki: `wiki/index.md`
  first, follow [[wikilinks]], prefer newest, cite item ids. File real syntheses
  back under `wiki/syntheses/`.
- **lint / health check** (`.claude/skills/dex-lint`) — `bin/dex lint`
  mechanical checks + judgment repairs: fix, fold in stale members,
  reconcile contradictions, re-apply `wiki/pins.md`. Runs automatically: a
  run triggers it when the last check is over 7 days old. Owners can also
  ask in plain words ("check my knowledge base over", "health check") —
  same operation.

Machinery is engine-owned: `CLAUDE.md`, `.claude/` (skills and this
contract), `bin/dex`, and `.gitattributes` are overwritten by `bin/dex sync`
— never hand-edit them here. Fixes go into the engine, then sync.

## The lens

`lens.md` at the instance root is this instance's lens: the owner's
statement of what the instance reads for, and the angle every judgment in
a run reads content through. It is free-form and owned by the owner, so
edit it only when the owner asks or a directive's instructions name it,
and read it as one coherent statement, never section by section.

An instance with no lens is a general knowledge dex, which is a legitimate
way to run one and never a fault to repair. When `lens.md` is missing or
empty, the instance reads for anything: every share is read for its
general substance, the facts the source yields, and the lens verdict drops
only content with nothing in it at all, such as chatter. A `lens.md` still
holding any of the seed's placeholder lines (the lines made only of text
in angle brackets, such as `<what to look at hardest>`) reads the same way
until the placeholders are replaced or the file is deleted, because a
placeholder line is never read as a lens, and so does a `lens.md` that
cannot be read. Lint and the sync report carry a note for those two cases,
which is the owner's to act on and never a session's repair.

Sharing something into this instance is the curation: the owner has
already decided it belongs here, so a session never rejects a share for
its subject, and a gardening site shared into a design instance is read
for its design. Everything behind a share is fetched and kept whatever the
lens, and harvest follows the item's subject as fully as it would anywhere
(the lens may add a page its angle needs, never skip one). The lens
decides what the item is read for: which facts its digest takes away, how
topics break apart and what the wiki writes about.
The only drop is an item whose landed content yields nothing through the
lens, such as a share into the wrong instance or chatter with nothing in
it; it goes through `bin/dex exclude` with its reason, and the run report
names it. Content the engine could not reach (a paywall, a blocked fetch,
a dead link) is a fetch problem for the parking lanes and never a drop,
and a parked item meets no verdict until its sources land.

Overlap between instances is expected: the same link shared into two
instances becomes two items, each read through its own lens, and nothing
in this instance routes content to a sibling.

## Dataflow

```
raw/ (verbatim exports) + inbox/ (capture files; staged binaries land in media/<id>/, LFS)
  →  corpus/ (one item per share, provenance frontmatter; media/ holds captured binaries)
  →  state/enrichment-ledger.jsonl (the pipeline's work queue — every fetch is an entry;
     harvest-promoted URLs live HERE, never in corpus frontmatter)
  →  enrichment/<id>/ (fetched content, transcripts, media descriptions)
  →  state/digests/<id>.md (fact index)
  →  state/taxonomy.json (topics/entities)  →  wiki/ (pages, index, log, pins)

state/ also holds: passes.jsonl (stage records) · migrations.jsonl (applied
migrations) · directives.jsonl (completed directives) · issue-reports.jsonl
(engine-defect reports, filed upstream or held locally — the gate or the
per-run cap) · config.json (owner-editable) · entity-members.json (entity →
items) · map.json (the compiled instance map — derived, rewritten with
wiki/index.md by `bin/dex map`) · exclusions.tsv
cache/ is ephemeral and gitignored (render payloads, in-flight audio) —
never state, never synced.
```

## Invariants (non-negotiable)

- An operation writes only inside this instance's root. Use absolute paths and
  verify the working directory before every write batch — a persisted `cd` must
  never land writes in a sibling instance or anywhere else.
- Provenance (who/where/when) is captured at ingest — it cannot be reconstructed later.
- `raw/` and `corpus/` are append-only. Corpus items are created by
  `bin/dex enrich item new` and fixed by re-running the engine, never by
  hand-editing (exclusions: `bin/dex exclude` + `state/exclusions.tsv`).
  Two sanctioned writers of existing items: the engine's own refresh (the
  `status`/`enrichment:` fields, derived from disk) and **engine
  migrations** rewriting engine-owned frontmatter fields (e.g. migration 1
  renaming `kinds:` vocabulary). The body — the owner's note — is verbatim
  forever.
- **State is written through verbs, never by hand.** Every `state/*.jsonl`
  is append-only and verb-written: ledger heals via `bin/dex enrich mark`,
  stage records via `bin/dex enrich pass`. Digests too: the judgment goes
  in as JSON and `bin/dex enrich item digest --file <payload>` writes the
  file, recording the digest pass in the same call. Media descriptions
  the same way: the text goes in as a file and `bin/dex enrich item
  describe <id> --of <file> --file <text>` takes the
  `enrichment/<id>/media-<n>.md` slot, refreshing the item's frontmatter
  in the same call. Engine-defect reports
  the same way: `bin/dex issue --file <payload>` files upstream and writes
  the `issue-reports.jsonl` record. Directive records too: `bin/dex
  directive done <n>` appends to `directives.jsonl` only once the
  directive's check passes. A hand-appended line
  is how state and reality diverge; the verbs are what make a malformed
  record impossible.
- `wiki/` is a build artifact: regenerable, never the only home of a fact. Pages cite
  corpus item ids in backticks — full ids, ALWAYS, and NEVER other wiki
  pages — so citations stay mechanically checkable.
- Coverage: every corpus item ends up cited by a page or explicitly ledgered as
  low-signal (`uncategorized-shares` in `state/taxonomy.json`). Nothing silently
  vanishes.
- Recency is first-class: pages lead with "Current state (as of ...)", keep
  superseded practice as marked history, and surface dated conflicts — the conflict
  is information.
- Human corrections live in `wiki/pins.md` (claim + anchor) and must be re-applied
  after any page regeneration.
- Update pages by rewrite-not-append: a page must always read as if written today.
- State-bearing reports and receipts render through surfaces (`bin/dex
  render`) — never hand-drawn. The one exception is a session's closing
  report to the owner: composed with judgment, its required content
  specified in the dex-run skill. Identity (item ids, URLs, paths) is
  always whole everywhere: never abbreviate one when quoting a report or
  writing a summary.
- Unattended sessions never edit `state/config.json`: proposed changes go in
  the run report; the owner ratifies them in an attended session.
- A directive (`bin/dex directive`) is the engine's decision, not the
  session's: written, reviewed and rehearsed before its release, then
  performed by the run after the pull. It always runs unattended and never
  asks the owner anything, and it has full authority over every file its
  instructions name, `state/config.json` included, so the rule above does
  not apply to it.

## Conventions

- Filenames lowercase kebab-case. Corpus items: `corpus/YYYY/YYYY-MM-DD-<slug>-<id>.md`
  (id = sha1 of a stable key, first 6 hex). Schema: `.claude/skills/dex-run/references/schema.md`.
- Citations: backticked full item ids inline. Wikilinks `[[name]]` only to names that
  exist in `state/taxonomy.json` or as pages.
- Commit and push after each completed operation with a one-line summary; append one line to
  `wiki/log.md` (`## [YYYY-MM-DD] <op> | <title>`).
- Multi-agent work (large backfills only): write work manifests BEFORE dispatching;
  every agent contract includes "if an input is missing, STOP — do not improvise";
  verify coverage mechanically after every wave.
