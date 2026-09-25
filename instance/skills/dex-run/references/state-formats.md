# Instance state formats

Everything under `state/` that the pipeline reads or writes, plus the
ephemeral `cache/`. All ids are corpus item ids. Judgment supplies the
values; the verbs write the files — digests, taxonomy and entity members
all land through their verbs and follow these shapes exactly — lint and
the wiki layer depend on them. The standing rule: state you reason over is
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
 "facts": ["one standalone fact per fact the source yields through this instance's lens, with concrete specifics, readable without the source in front of you",
           "..."]}
```

then `bin/dex enrich item digest --file cache/digest.json`. `signal` is
high | medium | low; topics and entities are canonical taxonomy names once
taxonomy exists, otherwise 2–5 kebab-case candidates; `entities` may be
omitted. `date` and `media:` are **not yours to pass** — the engine derives
both: the share date off the corpus item, and the media list from the
item's own `media:` followed by the media files sitting in
`enrichment/<id>/`. A payload naming either is refused. Rewriting is how a
revised judgment lands: run the verb again with the new payload and the
whole file is re-derived.

What it writes:

```markdown
---
id: <corpus item id>
date: 2026-08-18                  # the item's share date
signal: high                      # high | medium | low
topics: [agent-architecture]
entities: [claude-code]           # omitted when you name none
media:                            # omitted when the item has no media
  - media/<id>/photo.jpg          #   the item's own paths, then the
  - enrichment/<id>/media-0.png   #   media files in enrichment/<id>/
---
- one standalone fact bullet per fact the source yields through the lens.
```

`bin/dex lint` checks the same shape, and it checks three different things.
The frontmatter is a **hard failure**: a digest with no complete fence,
missing `id`/`date`/`signal`/`topics`, a `signal` outside high|medium|low,
empty `topics`, or an `id` that disagrees with its filename exits 1,
exactly like a malformed ledger line — the wiki layer reads these files,
and a digest that states no facts at all fails with them — an empty body is
the one thing the file exists not to be. How many facts beyond that is
**never checked**: the count measures the source, not the digest, and no
honest digest makes three facts out of a two-line tweet. `media:` is the
third, and it is an **advisory** — lint re-derives the listing and flags a
digest whose stated paths are not the same SET, drift in either direction
(order and list form are not drift; a path for a file since deleted is),
but never exits 1: re-emitting the digest is a session's act, and failing
the check on it would stop every scheduled run.

A digest that went through the verb cannot fail any of that, and cannot
drift the moment it lands — the check is a backstop for files that predate
the verb or were edited by hand, and the repair for every one of them is to
rewrite it through the verb — from a fresh reading of the item when the file
behind a stated path has been replaced, since the digest was read from
evidence the item no longer carries.

## `bin/dex issue` — session-observed engine bug reports

When the ENGINE misbehaves with nothing raised — output contradicting
state on disk, a documented behaviour that did not happen, a verb
writing the wrong thing — file it upstream through the verb (when to
file, and when never, is the "Engine defects" rubric in
`processing.md`, this directory). The observation goes in as JSON:

```json
{"verb": "enrich run",
 "expected": "one mechanics sentence: what the engine should have done",
 "observed": "one mechanics sentence: what it did instead",
 "steps": ["optional, short mechanics statements to reproduce"],
 "note": "optional fuller free text — never filed, local record only"}
```

then `bin/dex issue --file cache/issue.json`. Every public field is
mechanics, single-line and bounded: `verb` is the misbehaving command
from the real CLI vocabulary (`lint`, `enrich run`, `enrich item
digest`, …); `expected` and `observed` are one mechanics sentence each,
at most 90 characters; `steps` is at most 8 statements of at most 120
characters. The verb REFUSES the whole payload — files nothing, writes
nothing — when any public field contains a corpus item id, a URL, a
filesystem path, an email, or instance content (an instance-directory
segment or a `dex-*` name); the refusal names each field and what to
abstract. Rewrite the clause abstractly ("an item with two URLs", "the
config") and run it again — never try to sneak detail past the
detectors.

`note` is the other half of the split: free text, at most 2000
characters, newlines allowed, and NEVER filed. It lands only in this
instance's `state/issue-reports.jsonl` record, so the concrete detail —
ids, paths, the story — belongs there, for the owner to read and
forward by hand.

Dedup, the `report_issues` gate and failure behaviour match the crash
filer: the same defect files once and then comments or stays silent;
the gate stops only the upstream filing, and the local record still
lands (marked `filed: false`) so nothing observed is lost; `gh` trouble
is a stated line in the verb's output and never stops the run.

## `bin/dex exclude`: dropping items that yield nothing through the lens

Exclusion goes through the verb, never by deleting files. The batch is a
JSON list of records:

```json
[{"id": "<corpus item id>", "reason": "what it is, and why the lens finds nothing in it"}]
```

then `bin/dex exclude cache/exclusions.json`. A lens drop always states
its reason in lens terms, since the run report names it; left out, `reason`
defaults to "yields nothing through this instance's lens". The batch is
validated whole and refused whole — an id must be a corpus item id, never
a path — and one id twice collapses to one entry with the count stated.
The verb records each exclusion in `state/exclusions.tsv` (below); removes
the media files the item's corpus frontmatter lists under `media/`, and
their `media/<id>/` directory once it is empty; removes the corpus file,
`enrichment/<id>/` and `state/digests/<id>.md`; drops the item's records
from `state/passes.jsonl`; purges the item's ledger entries; and
recompiles `state/map.json` and `wiki/index.md`. The summary line states
every count. What another live item still claims stays: the ledger work it
shares, and a media file its own frontmatter lists (two captures of one
file name share one). Only the corpus says which media an item carries, so
the verb refuses the batch, naming the file, when a corpus file it cannot
read is one of the batch's own, or is any other while the batch carries
media; repair it and run the batch again. What
the purge leaves is the item's entry in `state/taxonomy.json` and any
membership in `state/entity-members.json` — placement ids are never
checked against the corpus, so lint's ghost-members row names each
leftover id and its list, and the removal is the session's: an `unplace`
payload through `bin/dex enrich place` (below).

## `bin/dex directive` — the engine's directives

A directive is work the engine ships for the run to perform on this
instance's own files: work that needs judgment, which a migration is not
allowed to do. Each has a number, a one-line intent, instructions, and a
check written in code. Sync lists the pending ones on its report, and the
run performs them after the pull (`preparation.md`, step 5). While any is
pending, the content commands (`bin/dex inbox`, `normalize`, `enrich` and
`exclude`) refuse before touching anything, exit non-zero and point at
`bin/dex directive list`. Every other command runs as usual.

- `bin/dex directive list` prints the pending directives in numeric
  order, each with its intent, and how to perform them, or says that none
  are pending.
- `bin/dex directive show <n>` prints the directive's intent and its full
  instructions.
- `bin/dex directive done <n>` runs the directive's check. When every
  condition holds it appends the record to `state/directives.jsonl`
  (below) and confirms, naming the commit message. When any condition is
  unmet it prints each one and what the run does next, writes nothing and
  exits non-zero. It also refuses, writing nothing, a number the engine
  does not ship and a directive whose predecessor is still pending, naming
  the one that comes first, because directives complete in numeric order.
  A directive already recorded is reported as such, and nothing is
  written.

The check confirms only what code can see: a file exists and is not
empty, config still parses, a link is present. Whether the work was
faithful rests on performing the instructions exactly.

## `state/taxonomy.json` — the topic and entity namespace

Topic and entity names are kebab-case and define the wikilink namespace: a
`[[name]]` is valid only if it exists here or as a wiki page.

**Never hand-write this file.** Placement is the judgment — what to file
where, when a topic exists at all — and it goes in as JSON:

```json
{"topics":   [{"name": "agent-architecture", "description": "One-sentence scope."}],
 "entities": [{"name": "claude-code", "kind": "tool", "raw": ["claude-md"]}],
 "place":    [{"id": "<item-id>", "topics": ["agent-architecture"],
               "entities": ["claude-code"]}],
 "unplace":  [{"id": "<item-id>", "topics": ["uncategorized-shares"]}],
 "drop":     {"topics": ["a-folded-away-topic"], "entities": []}}
```

then `bin/dex enrich place --file cache/placement.json`. Every key is
optional and the sections apply in that order: `topics`/`entities` upsert
definitions (create, or redescribe/redefine — a redefined topic keeps its
members, and an entity's `raw` always folds its canonical name in),
`place` adds memberships, `unplace` removes them (a sweep or split move
is place plus unplace in one payload), `drop` removes a whole definition
with its memberships. A topic's `items` is **not yours to pass** in a
definition — membership moves only through `place`/`unplace`. The payload
is validated whole and refused whole: an unknown key, a wrong type, a
name that is not kebab-case, placing into a topic or entity that neither
exists nor is defined in the same payload, unplacing what is not placed,
dropping what does not exist, or dropping `uncategorized-shares` — a
refusal writes nothing and names the field. Item ids are checked for
shape only, never against the corpus: placement may precede or follow
the item's other work, and ghost members are lint's finding. A
successful run rewrites this file and `state/entity-members.json`
deterministically — sorted names, sorted unique member ids and aliases —
and recompiles `state/map.json` and `wiki/index.md`. On the first
placement of a fresh instance the verb creates the files.

What it writes:

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

A digest's `topics:`/`entities:` and a taxonomy topic's `items` answer
different questions, and their disagreement is expected, permanent, and
load-bearing. The digest frontmatter is the classification made at digest
time, in the vocabulary available then — candidate names explicitly
allowed — and it is never rewritten when the taxonomy changes; the only
legitimate rewrite is re-running the digest verb because the *item* was
re-read. That staleness is the feature: topic sweeps and splits find
members by reading digests for names finer-grained than the current
taxonomy, and the taxonomy stays rebuildable from digests only because
classification lives in the permanent record. Taxonomy's `items` lists
are where each item is filed *today*. So the two records drifting apart
is not drift and never a repair — and stale-by-design digest frontmatter
is pipeline plumbing: never present it to the owner or an answering
agent as an item's current topics. The taxonomy is that answer.

## `state/entity-members.json` — entity → items

```json
{ "<entity-name>": ["<item-id>", "..."] }
```

Which items mention each entity; feeds entity pages. **Never hand-write
this file** — it is the other half of what `bin/dex enrich place` (above)
rewrites: `place`/`unplace` records with an `entities` list move the
memberships, dropping an entity removes its list, and an entity holds a
key only while it has members.

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
reference). Maintain page *bodies* by hand, and set `generated:` to today
whenever you rewrite one, since lint never moves an existing date; leave
`items:` to lint.

## `lens.md`: the instance's lens, owner-owned

`lens.md` at the instance root is the owner's statement of what this
instance reads for. It is free-form: the seed's headings (Reads for,
Emphasise, Set aside) are prompts the owner may write under in any form,
rename or delete, and nothing in the engine parses it by section.
Nothing loads it with a session's instructions: every step that judges
through it reads it, whole, as one coherent statement. It is the owner's,
so a session writes it only when the owner asks or a directive's
instructions name it, and `bin/dex sync` never writes it, whether it
exists or not.

A missing or empty `lens.md` makes the instance a general knowledge dex,
which is a legitimate way to run one: it reads for anything, every share
is read for its general substance, and the lens verdict drops only
content with nothing in it at all. A `lens.md` still holding any of the
seed's placeholder lines (the lines that are only `<...>`) reads the same
way until the placeholders are replaced or the file is deleted, since a
placeholder line is never read as a lens, and so does one that cannot be
read. `bin/dex lint` and the sync report carry a **Lens note** for those
two cases and never fail on it; a missing or empty `lens.md` gets no note
at all.

## `state/config.json` — instance configuration, owner-editable

Renamed from `normalize-config.json` (migration 1). Parsed loudly: an
unknown key is an error, never silently dead. Unattended runs NEVER edit
this file — they propose changes in the run report. That rule governs the
run's own judgment and does not apply to a directive whose instructions
name this file: a directive is the engine's decision, not the run's.

| key | meaning |
|---|---|
| `media_fetch` | `none` \| `lead` — media-stage URL downloads. `none` withholds the fetch only: media units are still ledgered and rest until the config changes |
| `transcribe_model` | whisper-local size (default `medium`) |
| `transcribe_base_url` / `transcribe_api_key` / `transcribe_api_model` | whisper-api (OpenAI-compatible) endpoint + model id; the key belongs in `.env`, not here |
| `instagram_base_url` | the Instagram embed proxy serving the media endpoints (default `https://uuinstagram.com`); the escape hatch when that host dies or you self-host a fork |
| `report_issues` | gate on filing engine bugs upstream — crash reports and `bin/dex issue` alike (default `true`). Gates the filing only: the `state/issue-reports.jsonl` record is written either way, `filed` saying which |
| `providers` | capability → provider order, e.g. `{"transcribe": ["whisper-api"]}` |
| `discord` | the Discord server and channels a pull exports: `{"guild": "<guild id>", "channels": {"<channel name>": "<channel id>"}}`, exactly those two keys and at least one channel. Ids are strings of digits, never JSON numbers; each channel name is the `raw/discord/<name>/` directory its export lands in. Absent means Discord is not configured (`backfills.md` sets it up); the token is `DISCORD_TOKEN` in `.env`, not here |
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
  attempts?, cap?, forced?, engine, date, at?, job?, via?, parent?, depth?,
  rerun?, http_shared?, path?, title?, error?, reason?}`. The latest line per hash wins, and
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
  skips it; `http_shared` marks a unit admitted from an http-spelled URL
  (a capture's `urls:` line, an `enrich fetch` argument) and licenses
  the fetch's TLS-failure fallback to plain http; `job` marks the units
  that are not fetched pages — `media`
  downloads (routed through the media stage's redrain) and
  extraction-`asset` byte-writes — while `via` is provenance only
  (`harvest`, `sniff`, `migration-<n>`) and never routes. Heals and
  manual resolutions:
  `bin/dex enrich mark` — it finds a unit by its canonical identity, or by
  the exact stored key for units recorded verbatim (bad seeds and every
  `job: media` line), so pass the URL as the ledger shows it and the heal
  lands on that entry.
- `state/passes.jsonl` — per-item stage records `{stage, item, date,
  rules?}` ("ran and promoted nothing" is distinguishable from "never
  ran"; `rules` versions the harvest rules). Written by
  `bin/dex enrich pass`; the digest pass is recorded by `enrich item
  digest` itself, in the same call as the file (`enrich pass --stage
  digest` remains the manual re-record). `bin/dex exclude` drops a dropped
  item's records, including those under its id from before a rename.
- `state/migrations.jsonl` — applied-migrations log `{number, engine,
  date}`. Written by sync's migration runner.
- `state/directives.jsonl` — completed-directives log `{number, engine,
  date}`, one record per directive this instance has performed. Written
  by `bin/dex directive done <n>` only after the directive's check passes
  (above), and seeded by `dex-new` with every directive the engine
  shipped when the instance was created, since a new instance is born in
  the shape directives exist to reach. Two machines that both performed
  one union-merge to two lines for one number, which reads the same as
  one.
- `state/issue-reports.jsonl` — what this instance observed and reported
  `{fingerprint, action, filed, engine, date, issue?, note?}`.
  Written by the issue filer (crash reports) and by `bin/dex issue`
  (session-observed reports, above); the owner's visible record.
  `action` is what the filing pass did: `filed` (a new upstream issue),
  `commented` (seen again on an open issue), `recorded` (local record
  only, the gate off), or `deferred` (local record only, the per-run
  filing cap reached). The record is written whether or not the report
  filed upstream — `filed: true|false` says which — and the two local
  actions mean different things: a gate-off `recorded` is seen for
  good, so turning the gate on later never auto-refiles what was
  observed while it was off, while a `deferred` report stays eligible
  and files the next time the observation recurs, budget allowing. At
  most one deferred record lands per fingerprint and engine.
  `note` exists only on records the issue verb wrote and is the local
  half of the privacy split: the fuller free-text context that is never
  part of the public issue — the owner reads it here and forwards what
  matters by hand.
- `state/exclusions.tsv` — one tab-separated `id<TAB>reason` line per
  excluded item, written by `bin/dex exclude` (payload above); excluded
  items stay excluded across re-normalization. Like the `state/*.jsonl`
  files it merges as a union across machines, and every reader answers
  by id, so a doubled ruling is harmless. The verb purges the item
  completely — its media, corpus file, `enrichment/<id>/`,
  `state/digests/<id>.md`, pass records and ledger entries — and states
  both the entry count it dropped and the count it kept because another
  live item still claims the work; git history keeps them.

## `cache/` — ephemeral, gitignored

Render payloads (`cache/*.json` for `bin/dex render`), in-flight audio
(`cache/audio/`). Never state, never synced; safe to delete between
sessions.
