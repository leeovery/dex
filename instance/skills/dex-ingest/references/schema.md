# Corpus item schema

One markdown file per item (a shared resource, a capture, or a pasted chunk of
knowledge). Body is the original message or note text, verbatim — including
surrounding commentary, which is often the most valuable part (why it mattered
to the person who saved it). Frontmatter:

```yaml
---
id: 2026-08-18-rag-eval-harness-a1b2c3      # YYYY-MM-DD-<slug>-<shortid>; shortid = sha1 of a stable key, first 6 hex
source: inbox                               # inbox | manual | <exporter name, e.g. discord>
channel: inbox                              # channel / space name; `inbox` for captures, `manual` for session drops
shared_by: Lee                              # display name as seen in the source
date: 2026-08-18                            # date shared (message timestamp, ISO)
urls:
  - https://www.youtube.com/watch?v=...
kinds: [youtube]                            # youtube | blog | github | paper | tweet | text | image | file
status: raw                                 # raw | enriched
enrichment: []                              # populated by enricher, e.g. [transcript.md]
media: []                                   # media captures: repo-relative paths (media/<id>/..., LFS-tracked)
---

<original message / note text, verbatim>
```

## Derivation rules

- `id` shortid — from a stable key: exporter message id for backfills;
  `manual/<canonical-url>` for link captures (`manual/<capture-filename>` when
  there's no URL); for media captures the id is fixed by `bin/dex inbox` (the
  `media/<id>/` directory name, sha1 of `media/<filename>`).
- `kinds` — from URL patterns; `text` when the body itself is the knowledge
  (no link, or the link is incidental); `image`/`file` when the item's primary
  source is captured media.
- `source` — `inbox` for capture files, `manual` for items dropped directly
  into a session, exporter name (e.g. `discord`) for backfills. Every source
  goes through the same pipeline and carries the same weight; nothing about a
  source implies age or importance.
- `status` — `raw` at creation; enricher flips to `enriched` and fills
  `enrichment` with filenames under `enrichment/<id>/`.
- Threaded replies that add substance are appended to the parent item's body
  with an attribution line, not split into separate items.
- The schema is identical across every instance — no instance-specific
  fields. Only the content differs.

## What is deliberately NOT in the schema

- No trust field — trust is derived at synthesis time from date + source + kind.
- No topic/tags — topics are the wiki layer's job; tagging at ingest bakes in a
  taxonomy before the corpus can reveal one.
