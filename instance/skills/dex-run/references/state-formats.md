# Instance state formats

Everything under `state/` that the pipeline reads or writes, plus the
ephemeral `cache/`. All ids are corpus item ids. Claude-written files
(digests, taxonomy, entity members) follow these shapes exactly — lint and
the wiki layer depend on them. The standing rule: state you reason over is
markdown; state machinery processes is JSONL/JSON; nothing binary in
`state/`.

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

## Wiki page frontmatter — derived fields, lint-repaired

Every topic/entity page opens with a frontmatter fence; syntheses carry
their own (`type: synthesis`, `question:`, `generated:` — see dex-query).
Bodies are the most freehand artifact in the system, on purpose; these
fields are derived and mechanically checkable:

```yaml
---
topic: agent-architecture     # topic pages: the taxonomy topic name
# — or —
entity: anthropic             # entity pages: the entity name
kind: org                     # entity pages may echo the entity kind
generated: 2026-08-18         # when the page content was last regenerated;
                              #   the staleness check compares member item
                              #   dates against it
items: 215                    # the page's MEMBER count — the taxonomy
                              #   topic's items length (topic pages) or the
                              #   entity-members list length (entity pages).
                              #   NOT the citation count: a page routinely
                              #   cites fewer items than its topic holds.
---
```

`bin/dex lint` verifies `items:` against the member count and flags drift;
`lint --write` reconciles it mechanically and adds a missing `generated:`
(existing `generated:` dates are never rewritten — they are the staleness
reference). Maintain page *bodies* by hand; leave these fields to lint.

## `state/config.json` — instance configuration, owner-editable

Renamed from `normalize-config.json` (migration 1). Parsed loudly: an
unknown key is an error, never silently dead. Unattended runs NEVER edit
this file — they propose changes in the run report.

| key | meaning |
|---|---|
| `media_fetch` | `none` \| `lead` — media-stage URL downloads |
| `transcribe_model` | whisper-local size (default `medium`) |
| `transcribe_base_url` / `transcribe_api_key` / `transcribe_api_model` | whisper-api (OpenAI-compatible) credentials + model id |
| `report_issues` | auto-file engine bugs upstream (default `true`) |
| `providers` | capability → provider order, e.g. `{"transcribe": ["whisper-api"]}` |
| `internal_domains` | domains treated as internal/noise at normalize |
| `noise_prefixes` | reserved |

## Engine-owned files (written through verbs, never by hand)

All `state/*.jsonl` files are append-only, full-record lines, merged as a
union between machines (`.gitattributes` sets `merge=union`). **Never
hand-append a line** — every write goes through a verb; that is what keeps
a malformed record impossible:

- `state/enrichment-ledger.jsonl` — the pipeline's work queue: one entry
  per unit of work `{hash, url, item, kind, format?, status, needs?,
  attempts?, engine, date, via?, parent?, depth?, rerun?, path?, title?,
  error?, reason?}`. Last line per hash wins; `bin/dex enrich compact`
  settles it. Statuses: queued · done · dead · skipped · manual · waiting ·
  blocked · error. `reason` is the stated parking reason (required on
  manual/skipped); `error` entries carry a scrubbed message and retry once
  per newer engine. Heals and manual resolutions: `bin/dex enrich mark`.
- `state/enrichment-ledger.unmigrated.jsonl` — the migration quarantine
  (normally ABSENT/empty): ledger lines a migration could not provably
  translate. Lint flags it non-empty; repair by reviewing each line and
  re-adding via `enrich mark`, then empty the file.
- `state/passes.jsonl` — per-item stage records `{stage, item, date,
  rules?}` ("ran and promoted nothing" is distinguishable from "never
  ran"; `rules` versions the harvest rules). Written by
  `bin/dex enrich pass`.
- `state/migrations.jsonl` — applied-migrations log `{number, engine,
  date}`. Written by sync's migration runner.
- `state/issue-reports.jsonl` — what this instance filed/commented
  upstream `{fingerprint, action, engine, date, issue?}`. Written by the
  issue filer; the owner's visible record.
- `state/exclusions.tsv` — written by `bin/dex exclude`; excluded items
  stay excluded across re-normalization.

## `cache/` — ephemeral, gitignored

Render payloads (`cache/*.json` for `bin/dex render`), in-flight audio
(`cache/audio/`). Never state, never synced; safe to delete between
sessions.
