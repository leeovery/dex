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
  migrations reviewed → pull → inbox → items → `enrich run` → per-item
  cognitive work for everything its report names (harvest → digest → place →
  wiki) → health check when due → push. Covers scheduled runs, "process
  now", and "process the inbox" alike.
- **query** (`.claude/skills/dex-query`) — answer from the wiki: `wiki/index.md`
  first, follow [[wikilinks]], prefer newest, cite item ids. File real syntheses
  back under `wiki/syntheses/`.
- **lint / health check** (`.claude/skills/dex-lint`) — `bin/dex lint`
  mechanical checks + judgment repairs: fix, fold in stale members,
  reconcile contradictions, re-apply `wiki/pins.md`. Runs automatically: a
  run triggers it when the last check is over 7 days old. Owners can also
  ask in plain words ("check my knowledge base over", "health check") —
  same operation.

Machinery is engine-owned: `.claude/` (skills and this contract), `bin/dex`,
and `.gitattributes` are overwritten by `bin/dex sync` — never hand-edit them
here. Fixes go into the engine, then sync.

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
migrations) · issue-reports.jsonl (bugs filed upstream) · config.json
(owner-editable) · exclusions.tsv
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
  stage records via `bin/dex enrich pass`. A hand-appended line is how
  state and reality diverge; the verbs are what make a malformed record
  impossible.
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
- Reports and receipts render through surfaces (`bin/dex render`) — never
  hand-drawn. Surface output is markdown, and identity in it (item ids,
  URLs, paths) is always whole: never abbreviate one when quoting a report.
- Unattended sessions never edit `state/config.json`: proposed changes go in
  the run report; the owner ratifies them in an attended session.

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
