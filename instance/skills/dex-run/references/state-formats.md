# Instance state formats

Everything under `state/` that the pipeline reads or writes, plus the
ephemeral `cache/`. All ids are corpus item ids. The files you write
(taxonomy, entity members) and the digests the verb writes follow these
shapes exactly — lint and the wiki layer depend on them. The standing rule: state you reason over is
markdown; state machinery processes is JSONL/JSON; nothing binary in
`state/`.

## `state/digests/<id>.md` — permanent per-item fact index

One per corpus item, written at digest time. Digests are permanent: pages
regenerate from digests, digests never regenerate from pages.

**Never hand-write this file.** Write the judgment as JSON and let the
engine serialize it:

```json
{"id": "<corpus item id>",
 "signal": "high",
 "topics": ["agent-architecture"],
 "entities": ["claude-code"],
 "facts": ["one standalone fact per fact the source actually yields, with concrete specifics, readable without the source in front of you",
           "..."]}
```

then `bin/dex enrich item digest --file cache/digest.json`. `signal` is
high | medium | low; topics and entities are canonical taxonomy names once
taxonomy exists, otherwise 2–5 kebab-case candidates; `entities` may be
omitted. `date` and `media:` are **not yours to pass** — they are the
item's own share date and media, the engine reads both off the corpus item,
and a payload naming either is refused. Rewriting is how a revised judgment
lands: run the verb again with the new payload and the whole file is
re-derived.

What it writes:

```markdown
---
id: <corpus item id>
date: 2026-08-18                  # the item's share date
signal: high                      # high | medium | low
topics: [agent-architecture]
entities: [claude-code]           # omitted when you name none
media:                            # only when the item has media
  - media/<id>/photo.jpg
---
- one standalone fact bullet per fact the source actually yields.
```

`bin/dex lint` checks the same shape, and it checks two different things.
The frontmatter is a **hard failure**: a digest with no complete fence,
missing `id`/`date`/`signal`/`topics`, a `signal` outside high|medium|low,
empty `topics`, or an `id` that disagrees with its filename exits 1,
exactly like a malformed ledger line — the wiki layer reads these files,
and a digest that states no facts at all fails with them — an empty body is
the one thing the file exists not to be. How many facts beyond that is
**never checked**: the count measures the source, not the digest, and no
honest digest makes three facts out of a two-line tweet.

A digest that went through the verb cannot fail any of that — the check is
a backstop for files that predate the verb or were edited by hand, and the
repair for one is to rewrite it through the verb.

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

- The coverage invariant lint enforces has two faces: every corpus item
  is cited by a page or ledgered in `uncategorized-shares`, and every
  cited item appears in at least one topic's `items`.
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
| `transcribe_base_url` / `transcribe_api_key` / `transcribe_api_model` | whisper-api (OpenAI-compatible) endpoint + model id; the key belongs in `.env`, not here |
| `report_issues` | auto-file engine bugs upstream (default `true`) |
| `providers` | capability → provider order, e.g. `{"transcribe": ["whisper-api"]}` |
| `internal_domains` | domains treated as internal/noise at normalize |
| `noise_prefixes` | reserved |

## `.env` — local secrets, gitignored

`KEY=VALUE` lines at the instance root (`#` comments skipped; one layer of
surrounding quotes stripped, so `KEY="sk-…"` and `KEY=sk-…` mean the same
thing). The enrich CLI folds it into the environment on every command, real
environment variables winning — so `OPENAI_API_KEY=…` here is how whisper-api gets its
key on this machine without the secret ever being committed. Config stays
for non-secrets (base_url, model names).

## Engine-owned files (written through verbs, never by hand)

All `state/*.jsonl` files are append-only, full-record lines, merged as a
union between machines (`.gitattributes` sets `merge=union`). **Never
hand-append a line** — every write goes through a verb; that is what keeps
a malformed record impossible:

- `state/enrichment-ledger.jsonl` — the pipeline's work queue: one entry
  per unit of work `{hash, url, item, kind, format?, status, needs?,
  attempts?, cap?, forced?, engine, date, at?, via?, parent?, depth?,
  rerun?, path?, title?, error?, reason?}`. The latest line per hash wins, and
  latest means the newest `at` — the UTC write instant every line carries —
  not the last line in the file, because a union merge between two machines
  concatenates their lines in git's order, not in write order. Lines
  written before `at` shipped carry none and count as oldest. `bin/dex
  enrich compact` settles the file down to the winners.
  Statuses: queued · done · dead · skipped · manual ·
  waiting · blocked · error. `reason` is the stated parking reason
  (required on manual/skipped); `error` entries carry a scrubbed message
  and retry once per newer engine; `cap` marks a skip that records
  cap-refused work, not an admitted unit, and names the bound that refused
  it (`depth`, or `url-requested` — the per-item URL budget an `enrich
  fetch` may exceed with `--force`); `forced` marks the fire `--force`
  waived — the unit still entered, and the health check's drift reading
  skips it. Heals and manual resolutions:
  `bin/dex enrich mark` — it finds a unit by its canonical identity, or by
  the exact stored key for units recorded verbatim (bad seeds and every
  `via: media` line), so pass the URL as the ledger shows it and the heal
  lands on that entry.
- `state/passes.jsonl` — per-item stage records `{stage, item, date,
  rules?}` ("ran and promoted nothing" is distinguishable from "never
  ran"; `rules` versions the harvest rules). Written by
  `bin/dex enrich pass`; the digest pass is recorded by `enrich item
  digest` itself, in the same call as the file (`enrich pass --stage
  digest` remains the manual re-record).
- `state/migrations.jsonl` — applied-migrations log `{number, engine,
  date}`. Written by sync's migration runner.
- `state/issue-reports.jsonl` — what this instance filed/commented
  upstream `{fingerprint, action, engine, date, issue?}`. Written by the
  issue filer; the owner's visible record.
- `state/exclusions.tsv` — written by `bin/dex exclude`; excluded items
  stay excluded across re-normalization. The verb purges the item
  completely — corpus file, `enrichment/<id>/`, `state/digests/<id>.md`,
  and the item's ledger entries — and states both the entry count it
  dropped and the count it kept because another live item still claims
  the work; git history keeps them.

## `cache/` — ephemeral, gitignored

Render payloads (`cache/*.json` for `bin/dex render`), in-flight audio
(`cache/audio/`). Never state, never synced; safe to delete between
sessions.
