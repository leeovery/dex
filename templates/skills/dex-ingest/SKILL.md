---
name: dex-ingest
description: Ingest new knowledge into this dex instance — capture files in state/inbox/, a URL or text dropped into the session, or exports in raw/. Use when the user says "add this to dex", "ingest", or when the inbox has captures waiting.
---

# Ingest

One item = capture provenance → enrich → digest → place → update wiki. Never skip
steps; never invent inputs. All commands run from the instance root.

## The inbox

`state/inbox/` holds one file per capture, written directly by capture clients
(the Send To Dex shortcut, or anything else that PUTs via the GitHub contents
API):

- **Text capture** — a `.md` file whose body is a URL and/or a note.
- **Image capture** — an image file (`.jpg`, `.png`, ...), optionally with a
  `.md` sidecar of the same stem carrying the note.

`git pull` first — captures arrive as commits. Process every file, then delete
it (the capture is preserved in git history; its content lives on in the
corpus).

## Per text/URL capture

1. **Scope check** against CLAUDE.md's In-scope list. Unsure → ask the owner.
   Rejected → delete the capture file and say why.
2. **Corpus item.** Canonicalize the URL (strip utm/tracking params). Compute
   `shortid = sha1("manual/<canonical-url>")[:6]`. Write
   `corpus/YYYY/YYYY-MM-DD-<slug>-<shortid>.md` with frontmatter per the
   engine's `docs/schema.md`: id, source (`inbox`|`manual`|exporter name),
   channel, shared_by, date, urls, kinds, `status: raw`, `enrichment: []`.
   Body = the capture's note verbatim — often the most valuable part.
3. **Enrich:** `bin/kb enrich run` (fetches captions, article text, READMEs,
   papers, tweets — and lead images/tweet photos into
   `enrichment/<id>/media-N.*` unless the instance config says otherwise).
   Caption-less video/audio: `bin/kb enrich whisper` (OPENAI_API_KEY in `.env`).
4. **Digest.** Read the enrichment fully — including viewing any fetched media
   files — and write `state/digests/<id>.md`: frontmatter (id, date,
   `signal: high|medium|low`, topics, entities, and `media:` listing any media
   paths) + 3–15 standalone fact bullets with concrete specifics. Topics: use
   canonical names from `state/taxonomy.json` when it exists; otherwise 2–5
   kebab-case candidates.
5. **Place.** Taxonomy exists → append the id to each matching topic's items.
   Create a new topic only once several items justify a page.
6. **Update the wiki.** Splice cited sentence(s) into affected pages —
   rewrite-not-append if "current state" changes. Pages may embed item media
   with relative image links where showing beats describing. Update
   `wiki/index.md`; append a `wiki/log.md` line; delete the capture file.
7. **Commit + push**: `ingest: <short title>`.

## Per image capture

Images are corpus items whose primary source is the image itself.

1. **Scope check** — view the image first; judge content against the In-scope
   list.
2. **File the media.** Move the image to `media/<item-id>/<original-name>`
   (create the item id first: `shortid = sha1("media/<inbox-filename>")[:6]`,
   slug from what the image shows). `media/` is append-only and LFS-tracked.
3. **Corpus item** as above, with `kinds: [image]`, a `media:` frontmatter list
   pointing at the file(s), and the sidecar note (if any) as the body.
4. **Describe.** View the image and write `enrichment/<id>/media-0.md`: what it
   depicts, all legible text (OCR), and — where relevant to this instance's
   domain — style, palette, composition, typography, layout. This is the
   image's "transcript"; make it substantive enough to stand in for the image
   in text-only contexts.
5. **Digest, place, wiki, commit** as steps 4–7 above. Wiki pages citing an
   image item should usually embed it.

## Backfills (exports in raw/)

`bin/kb normalize` → scope-filter pass (judgment; purge via `bin/kb exclude
<file.json>`) → `bin/kb enrich run` → digest/place/assemble at scale. For 500+
items: work in waves; write work manifests BEFORE dispatching any parallel
agents; every agent contract includes "if an input is missing, STOP — do not
improvise"; verify coverage mechanically between waves.
