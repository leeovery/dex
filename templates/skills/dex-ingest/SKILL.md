---
name: dex-ingest
description: Ingest new knowledge into this dex instance — from state/inbox.md, a URL or text dropped into the session, or exports in raw/. Use when the user says "add this to dex", "ingest this", or when inbox lines are waiting.
---

# Ingest

One item = capture provenance → enrich → digest → place → update wiki. Never skip
steps; never invent inputs. All commands run from the instance root.

## Per item

1. **Scope check** against CLAUDE.md's In-scope list. Unsure → ask the owner. Rejected
   inbox line → remove it and say why.
2. **Corpus item.** Canonicalize the URL (strip utm/tracking params). Compute
   `shortid = sha1("manual/<canonical-url>")[:6]`. Write
   `corpus/YYYY/YYYY-MM-DD-<slug>-<shortid>.md` with frontmatter per the engine's
   `docs/schema.md`: id, source (`inbox`|`manual`|exporter name), channel,
   shared_by, date, urls, kinds, `status: raw`, `enrichment: []`. Body = the
   sharer's verbatim note/context — often the most valuable part.
3. **Enrich:** `bin/kb enrich run` (fetches captions, article text, READMEs,
   papers, tweets; updates the item's frontmatter). Caption-less video/audio:
   `bin/kb enrich whisper` (needs OPENAI_API_KEY in the instance's `.env`).
4. **Digest.** Read the enrichment fully; write `state/digests/<id>.md`:
   frontmatter (id, date, `signal: high|medium|low`, topics, entities) + 3–15
   standalone fact bullets with concrete specifics — names, numbers, versions,
   dates ("as of YYYY-MM"). Topics: use canonical names from
   `state/taxonomy.json` when it exists; otherwise 2–5 kebab-case candidates.
5. **Place.** Taxonomy exists → append the id to each matching topic's items (and
   entity members where tracked). Create a new topic only once several items
   justify a page.
6. **Update the wiki.** Splice cited sentence(s) into affected pages —
   rewrite-not-append if "current state" changes; mark superseded claims rather
   than deleting them. New pages only for topics with enough members. Update
   `wiki/index.md`; append a `wiki/log.md` line; clear the inbox line.
7. **Commit + push**: `ingest: <short title>`.

## Backfills (exports in raw/)

`bin/kb normalize` → scope-filter pass (judgment; purge via `bin/kb exclude
<file.json>`) → `bin/kb enrich run` → digest/place/assemble at scale. For 500+
items: work in waves; write work manifests BEFORE dispatching any parallel
agents; every agent contract includes "if an input is missing, STOP — do not
improvise"; verify coverage mechanically between waves.
