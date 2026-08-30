# Corpus item schema

One markdown file per item (a shared resource, a capture, or a pasted chunk of
knowledge). Body is the original message or note text, verbatim — including
surrounding commentary, which is often the most valuable part (why it mattered
to the person who saved it). Items are created by `bin/dex enrich item new`,
never freehand — code writes frontmatter, prose stays yours. Frontmatter:

```yaml
---
id: 2026-08-18-rag-eval-harness-a1b2c3      # YYYY-MM-DD-<slug>-<shortid>; shortid = sha1 of a stable key, first 6 hex
source: inbox                               # inbox | manual | <exporter name, e.g. discord>
channel: inbox                              # channel / space name; `inbox` for captures
shared_by: Alex                             # display name as seen in the source
date: 2026-08-18                            # date shared (capture timestamp / message timestamp, ISO)
urls:
  - https://www.youtube.com/watch?v=...     # capture provenance — what was SHARED, verbatim;
                                            #   immutable after ingest (promoted URLs live in the ledger only)
kinds: [youtube]                            # youtube | x | instagram | github | paper | podcast | web | file | image | text
attachments:                                # backfill exports only: provenance — the exporter's own
  - raw/discord/general/assets/photo.png    #   paths under raw/, materialized into media/<id>/ by normalize
reactions: 5                                # backfill exports only: total reaction count, when > 0
status: raw                                 # raw | enriched — engine-owned
enrichment: []                              # engine-owned: filenames under enrichment/<id>/
media:                                      # media captures and materialized backfill attachments:
  - media/a1b2c3/20260818-101530.jpg        #   repo-relative paths (media/<id>/..., LFS-tracked)
---

<original message / note text, verbatim>
```

## Derivation rules

- `id` shortid — from a stable key, computed by the engine: exporter message
  id for backfills; `manual/<canonical-url>` for link captures
  (`manual/<capture-filename>` when there's no URL); for media captures the
  id is fixed by `bin/dex inbox` (the `media/<id>/` directory name, sha1 of
  `media/<filename>`).
- `kinds` — pattern-only detection at creation, **provisional forever**: the
  enrichment ledger is authoritative for what a URL actually was. `text` when
  the body itself is the knowledge; `image`/`file` when the item's primary
  source is captured media (`file` = a document format the extract queue
  handles).
- `source` — `inbox` for captures (the dex-capture skill writes the same
  files the phone shortcut does), exporter name (e.g. `discord`) for
  backfills. Every source goes through the same pipeline and carries the
  same weight; nothing about a source implies age or importance.
- `status` / `enrichment` — engine-owned and derived, never hand-edited: the
  listing from the files under `enrichment/<id>/`, the status from the
  ledger — `enriched` only once every unit the item owns has landed (or is
  confirmed gone or deliberately skipped), `raw` while any is still owed.
  An item with no units derives its status from the enrichment directory
  instead (`raw` while it is empty, `enriched` once anything is there);
  owed description and digest are the run report's and the digest
  backstop's to carry, never the status's.
  After creation these are the ONLY two frontmatter fields that ever change.
- `urls` is immutable capture provenance. Harvest-promoted URLs are ledger
  entries (`via: harvest`), never frontmatter edits.
- `media` — the files the item's own work units are seeded from: capture media
  as `bin/dex inbox` materialized it, and for backfills each local attachment
  copied out of the export into `media/<item shortid>/<file name>`. Derived at
  normalize time, so a re-run heals it; `attachments` is provenance only.
- Threaded replies that add substance are appended to the parent item's body
  with an attribution line, not split into separate items (backfills; for
  X threads the driver's walk-up puts thread context in the enrichment).
- The schema is identical across every instance — no instance-specific
  fields. Only the content differs.

## What is deliberately NOT in the schema

- No trust field — trust is derived at synthesis time from date + source + kind.
- No topic/tags — topics are the wiki layer's job; tagging at ingest bakes in a
  taxonomy before the corpus can reveal one.
