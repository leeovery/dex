# Corpus item schema

One markdown file per shared item (a message containing one or more resources, or a
pasted chunk of knowledge). Body is the original message text, verbatim. Frontmatter:

```yaml
---
id: 2025-06-12-rag-eval-harness-a1b2        # YYYY-MM-DD-<slug>-<shortid>; shortid from source message id
source: discord                             # discord | gspace | manual
channel: ai-resources                       # channel / space name
shared_by: <sharer>                           # display name as seen in the source
date: 2025-06-12                            # date shared (message timestamp, ISO)
urls:
  - https://www.youtube.com/watch?v=...
kinds: [youtube]                            # derived from urls: youtube | blog | github | paper | tweet | text | image
tier: frontier                              # foundations | frontier — derived, see below
status: raw                                 # raw | enriched
enrichment: []                              # populated by enricher, e.g. [transcript.md]
media: []                                   # optional: repo-relative image/file paths (media/<id>/..., LFS-tracked)
---

<original message text, verbatim — including surrounding commentary, which is often
the most valuable part (why the sharer thought it mattered)>
```

## Derivation rules

- `kinds` — from URL patterns; `text` when the message body itself is the knowledge
  (no link, or the link is incidental).
- `tier` — `gspace` → `foundations`; `discord` and `manual` → `frontier`. Refine by
  date later if needed; keep the rule dumb until it's wrong.
- `shared_by: <departed-account label>` — a departed account. Takeout strips all identifiers from
  deleted users, so leavers can't be told apart; in the two AI spaces this is *mostly*
  <sharer> (he was the prolific sharer) but not provably on any given item.
- `manual` items — anything <owner> drops in directly (URL or pasted text, via a session or
  `bin/kb add`). `shared_by: <owner>`, `channel: manual`, date = today. Same pipeline from
  there on.
- `status` — `raw` at normalize time; enricher flips to `enriched` and fills
  `enrichment` with filenames under `enrichment/<id>/`.
- Threaded replies that add substance are appended to the parent item's body with an
  attribution line, not split into separate items.

## What is deliberately NOT in the schema

- No trust field — trust is derived at synthesis time from date + source + kind.
- No topic/tags — topics are the wiki layer's job; tagging at ingest bakes in a
  taxonomy before the corpus can reveal one.
