# The instance map

> Designed 2026-09-01, out of a working conversation with the owner — no
> backlog file preceded it. Not yet implemented. Divergence numbers below
> were measured against dex-engineering on 2026-08-31 and describe that
> moment, not a contract.

Topics and entities become first-class citizens of the query surface,
alongside pages: a chat client can ask any instance for its full topic
list (each with its one-line description and metadata), its entity list,
and a relation graph — in one tool call each, served from a cached file,
with no call-time derivation and no per-page crawling. Behind that
surface, the files the answers come from stop being model-maintained
prose and become mechanically compiled artifacts.

## Why

The thin query surface (its own design) shipped `search`/`fetch`/`page`
plus two MCP resources per instance — `wiki/index.md` and
`state/taxonomy.json` — as the corpus map. Three problems surfaced in
use:

1. **Resources don't reach chat clients.** Claude Code reads MCP
   resources autonomously (verified in session); Claude Desktop chat
   only offers them for manual attachment, and Cowork is undocumented.
   Tools are the one surface every client honours. The maps therefore
   need a tool path.
2. **The index is model-written.** `wiki/index.md` duplicates taxonomy
   content (descriptions, counts) in prose that sessions maintain by
   hand, with lint as a drift backstop. For load-bearing catalog data
   the owner's ruling is: mechanical derivation, judgment only where
   judgment is irreducible.
3. **A graph had no honest path.** Every `Page` result already returns
   its outgoing wikilinks, so the full relation graph was reachable only
   by crawling every page — hundreds of calls. The data is mechanically
   derivable; it was never aggregated.

## Canonical source: taxonomy keeps membership, and gains custody

`state/taxonomy.json` stays the canonical record of topics (description
+ member item ids) and entities (kind + folded aliases), with
`state/entity-members.json` for entity→items. Its shape is unchanged.

The considered alternative — derive membership mechanically by inverting
digest frontmatter (`topics:`/`entities:` per item) — was measured and
rejected. In dex-engineering, 2,532 digests against 94 topics: 2,293
membership pairs agree, ~4,800 disagree, and 3,109 digest topic names
are not canonical taxonomy names at all. That is not drift (see the next
section); it means digests record a different fact than placement does,
and cannot be the membership source. Beyond the measurement, the
principled reason stands on its own: placement is a *corpus-level*
judgment — topics split, merge, and get swept — and encoding it only in
thousands of per-item files would scatter a relational decision across
the corpus and make every sweep a mass rewrite. Membership is relational
data; it lives in the relation file. `uncategorized-shares` (a ledger,
not a classification) also has no honest per-item home.

What changes is custody: taxonomy gets a mechanical write verb mirroring
the digest verb — the session states the judgment as a JSON payload, the
engine validates it, serializes it, and orders it deterministically.
"Never hand-write this file" extends to `state/taxonomy.json` and
`state/entity-members.json`. Authorship stays cognitive; serialization
becomes the engine's.

## Digest classification vs taxonomy placement (settled semantics)

The two `topics:` records answer different questions, and their
disagreement is expected, permanent, and load-bearing:

- A **digest's** `topics:`/`entities:` frontmatter is the classification
  made at digest time, in the vocabulary available then — candidate
  names explicitly allowed. It is never rewritten when the taxonomy
  changes; the only legitimate rewrite is re-running the digest verb
  because the *item* was re-read. The staleness is the feature: topic
  sweeps and splits find members by reading digests for names
  finer-grained than the current taxonomy (the 3,109 candidate names are
  the raw material future topics are built from), and taxonomy is
  rebuildable from digests only because classification lives in the
  permanent record.
- **Taxonomy's** `items` lists are where each item is filed *today*.

This was investigated to be sure it was intent and not accident: the
non-updating follows necessarily from the digests-are-permanent rule and
is exploited by the sweep and split procedures, but no document had ever
stated the consequence. The statement now lives in the engine CLAUDE.md
and lands in `state-formats.md` with this implementation. Corollary, owner's ruling: stale-by-design frontmatter
is pipeline plumbing and must never surface to a human or an answering
agent as an item's current topics — see the serve hygiene section.

One genuine gap the measurement exposed: 36 pairs where a digest names a
*canonical* topic and taxonomy does not record the item — each either a
placement miss or a topic-rename residue. That class gets a lint
advisory (below); the repairs are session judgment.

## The compiled map

One new mechanical compile reads taxonomy, entity-members, the corpus,
and the wiki, and writes one cached artifact per instance —
`state/map.json`:

- **topics**: name, description, item count, member item ids, whether a
  page exists, newest member's share date. `uncategorized-shares` is
  excluded — it is a ledger, not a topic.
- **entities**: name, kind, aliases, item count, member item ids,
  whether a page exists.
- **graph**: nodes are topics and entities; edges are typed:
  - `wikilink` — directed, page A's body links `[[B]]`. The curated
    relation; extraction already exists in the serve layer.
  - `shared-items` — undirected, weighted by membership intersection,
    topic↔topic and entity↔topic. The statistical relation prose never
    got around to linking.

  Item counts size nodes; weights size edges. The whole graph is
  compile-time inversion of existing files — zero new authorship.

**Ids in the artifact, counts on the wire.** The map carries full member
id lists (it is a cached file; the size is taxonomy-order), but the
tools return counts — ninety-odd topics times full id lists would bury a
chat model. A topic's *curated* membership is its page's citations via
`page()`; a future need for raw ids is a tool change, never a compile
change.

**Triggers.** The compile runs as a side-effect of every engine verb
that changes an input, and as a required end-of-run step. Wiki pages are
session-edited directly (no verb), so wikilink edges are only as fresh
as the last compile — the end-of-run step plus lint freshness is the
honest bound.

**Freshness enforcement.** Lint recompiles and diffs: a stale map is a
finding, and the health-check repair is rerunning the compile. The new
advisory row joins it: digest names a canonical topic whose `items` do
not record the id.

## The index becomes a rendered surface

`wiki/index.md` is regenerated mechanically from the map through the
render kernel — judgment decides (descriptions, placement, in
taxonomy), code renders. The model-written index and its whole drift
class are deleted; dex-query's "index first" flow is unchanged. Being a
rendered surface, the index is declared unpinnable: `wiki/pins.md`
targets pages' claims, never the catalog.

## The serve surface

Three new tools, each a verbatim read of `state/map.json`:

- `topics(instance)` — every topic: name, description, item count,
  has-page, newest date.
- `entities(instance)` — every entity: name, kind, aliases, item count,
  has-page.
- `graph(instance, around=None, min_weight=None, full=False)` — typed,
  weighted edges. Amended 2026-09-01 after driving the compile against a
  copy of the owner's production instance: the full graph there is 14,142
  edges — 1.3MB, roughly 350k tokens — which no chat model can hold, and
  a third of it is weight-1 shared-items noise. The limit is a default,
  never a ceiling: the bare call serves the topic↔topic core under an
  edge cap (every wikilink edge kept, the heaviest shared-items filling
  the room, the whole count always stated with the widening knobs named);
  `around=<name>` is one name's neighborhood, all types and weights,
  uncapped (~280 edges / 23KB there); `min_weight` drops light
  shared-items ties; `full=true` is the whole graph, uncapped, for the
  caller who insists. The untrimmed artifact's real consumers are code,
  not a model's context — a Code session reads `state/map.json` directly
  (e.g. to embed the data in a visualization artifact), so the file also
  ships as a third MCP resource.

Tools, not resources, because of client reality (Why, item 1). The three
resources — index, taxonomy, and now the compiled map — serve the
clients that can read them.

**Exposure hygiene** — one rule: judgment content and provenance
surface; pipeline bookkeeping and stale-by-design frontmatter never do.
Three fixes fall out:

- `fetch` returns the digest **body** (the facts), with `signal` lifted
  to a proper Item field — it is a current judgment worth keeping. The
  frontmatter's stale topics/entities no longer reach callers.
- `search` scans digest **bodies** only. Accepted trade: searching a
  canonical topic name no longer finds items via digest frontmatter —
  the tools, the taxonomy, and topic pages are the authoritative
  membership paths.
- `page` returns the **body**, frontmatter stripped — the serve layer
  already excludes page frontmatter from snippets as "`generated:`
  bookkeeping"; returning it verbatim from `page()` contradicted that.

Unchanged and deliberate: corpus frontmatter stays searchable (URLs are
provenance, and finding an item by its source is a real search);
enrichment texts, page bodies, and the owner's verbatim notes surface as
they are.

## How existing instances catch up

Split by nature:

- **Derived state migrates.** A numbered migration builds `map.json` and
  renders `index.md` at sync time — both are mechanical derivations of
  files every instance already has, so the new tools work the moment an
  instance syncs, not after its next run. Thereafter the compile
  triggers keep them fresh.
- **The verb needs no catch-up.** Taxonomy's shape is unchanged;
  sessions start writing through the verb when the synced skills say so.
- **Judgment self-heals.** The 36-pair advisory class cannot be
  migrated — miss or rename is a call. It surfaces at the next health
  check and heals through the normal repair cycle.

## Deferred, deliberately

- Exposing member id lists through the tools (the map already carries
  them).
- Any richer topic metadata — add to the compile when a consumer exists;
  the artifact is cheap to extend.
