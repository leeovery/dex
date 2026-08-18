# <instance> — <Domain> Knowledge Base (a dex instance)

A personal, LLM-maintained knowledge base. You (Claude) ARE the application: you
operate this repo per the contract below. Engine (mechanical pipeline):
github.com/leeovery/dex, run via `bin/kb <cmd>` from the repo root.

## In scope

- <topic>
- <topic>

Anything not listed is out of scope. When unsure, ask the owner rather
than guessing. The list is mirrored in README.md — any scope change must
update both files.

## Operations

Three operations. Detailed procedures live in skills — load them, don't improvise:

- **ingest** (`.claude/skills/dex-ingest`) — new items from `state/inbox/` capture files, a URL
  dropped in session, or exports in `raw/`. Capture provenance → enrich → digest →
  place → update affected wiki pages, index, log.
- **query** (`.claude/skills/dex-query`) — answer from the wiki: `wiki/index.md`
  first, follow [[wikilinks]], prefer newest, cite item ids. File real syntheses
  back under `wiki/syntheses/`.
- **lint / health check** (`.claude/skills/dex-lint`) — `bin/kb lint` mechanical
  checks + judgment: fix, fold in stale members, reconcile contradictions,
  re-apply `wiki/pins.md`. Owners will ask for this in plain words ("check my
  knowledge base over", "health check") — same operation.

## Dataflow

```
raw/ (verbatim exports) + state/inbox/ (capture files; staged binaries land in media/<id>/, LFS)
  →  corpus/ (one item per share, provenance frontmatter; media/ holds captured binaries)
  →  enrichment/<id>/ (fetched content + media descriptions)
  →  state/digests/<id>.md (fact index)
  →  state/taxonomy.json (topics/entities)  →  wiki/ (pages, index, log, pins)
```

## Invariants (non-negotiable)

- Provenance (who/where/when) is captured at ingest — it cannot be reconstructed later.
- `raw/` and `corpus/` are append-only. Fix normalizer bugs by re-running, never by
  hand-editing corpus items.
- `wiki/` is a build artifact: regenerable, never the only home of a fact. Pages cite
  corpus item ids in backticks — NEVER other wiki pages — so citations stay
  mechanically checkable.
- Coverage: every corpus item ends up cited by a page or explicitly ledgered as
  low-signal. Nothing silently vanishes.
- Recency is first-class: pages lead with "Current state (as of ...)", keep
  superseded practice as marked history, and surface dated conflicts — the conflict
  is information.
- Human corrections live in `wiki/pins.md` (claim + anchor) and must be re-applied
  after any page regeneration.
- Update pages by rewrite-not-append: a page must always read as if written today.

## Conventions

- Filenames lowercase kebab-case. Corpus items: `corpus/YYYY/YYYY-MM-DD-<slug>-<id>.md`
  (id = sha1 of a stable key, first 6 hex). Schema: engine `docs/schema.md`.
- Citations: backticked item ids inline. Wikilinks `[[name]]` only to names that
  exist in `state/taxonomy.json` or as pages.
- Commit after each completed operation with a one-line summary; append one line to
  `wiki/log.md` (`## [YYYY-MM-DD] <op> | <title>`).
- Multi-agent work (large backfills only): write work manifests BEFORE dispatching;
  every agent contract includes "if an input is missing, STOP — do not improvise";
  verify coverage mechanically after every wave.
