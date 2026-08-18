# Instance state formats

Everything under `state/` that the pipeline reads or writes. All ids are corpus
item ids. Claude-written files (digests, taxonomy, entity members) follow these
shapes exactly — lint and the wiki layer depend on them.

## `state/digests/<id>.md` — permanent per-item fact index

One per corpus item, written at digest time. Digests are permanent: pages
regenerate from digests, digests never regenerate from pages.

```markdown
---
id: <corpus item id>
date: 2026-08-18                  # the item's share date
signal: high                      # high | medium | low
topics: [agent-architecture]      # canonical taxonomy names once taxonomy exists;
entities: [claude-code]           #   otherwise 2–5 kebab-case candidates
media: [media/<id>/photo.jpg]     # only when the item has media
---
- 3–15 standalone fact bullets with concrete specifics, each readable without
  the source in front of you.
```

## `state/taxonomy.json` — the topic and entity namespace

Topic and entity names are kebab-case and define the wikilink namespace: a
`[[name]]` is valid only if it exists here or as a wiki page.

```json
{
  "topics": {
    "agent-architecture": {
      "description": "One-sentence scope of the topic.",
      "items": ["<item-id>", "..."]
    },
    "uncategorized-shares": {
      "description": "Ledger for low-signal items not worth a page.",
      "items": ["<item-id>", "..."]
    }
  },
  "entities": {
    "claude-code": {
      "kind": "tool",
      "raw": ["claude-code", "claude-md", "claude-skills"]
    }
  }
}
```

- Every corpus item must appear in at least one topic's `items` or in
  `uncategorized-shares` — that's the coverage invariant lint enforces.
- `entities.<name>.kind`: tool | company | person | model | concept (extend
  with judgment). `raw` lists the aliases folded into the canonical name.

## `state/entity-members.json` — entity → items

```json
{ "<entity-name>": ["<item-id>", "..."] }
```

Which items mention each entity; feeds entity pages.

## Engine-owned files (never hand-edit)

- `state/enrichment-ledger.jsonl` — one JSON line per enrichment outcome,
  written by `dex-enrich` (including deliberate skips, so nothing is silently
  unfetched).
- `state/exclusions.tsv` — written by `bin/dex exclude`; excluded items stay
  excluded across re-normalization.
- `state/normalize-config.json` — instance configuration, owner-editable:

```json
{
  "name_map": {},            "//": "raw sharer name -> display name",
  "internal_domains": [],    "//": "domains treated as internal/noise",
  "noise_prefixes": [],      "//": "message prefixes to drop at normalize",
  "media_fetch": "lead"      "//": "none | lead — fetch lead images at enrich"
}
```

(JSON has no comments — the `//` keys above are annotation in this doc only.)
