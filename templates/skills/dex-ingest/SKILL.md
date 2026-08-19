---
name: dex-ingest
description: Ingest new knowledge into this dex instance — capture files in inbox/, a URL or text dropped into the session, or exports in raw/. Use when the user says "add this to dex", "ingest", or when the inbox has captures waiting.
---

# Ingest

One item = capture provenance → enrich → digest → place → update wiki. Never skip
steps; never invent inputs. All commands run from the instance root.

## The inbox

`inbox/` holds one `.md` file per capture, written directly by capture
clients (the Send To Dex shortcut, or anything else that PUTs via the GitHub
contents API). Every capture has the same shape: the body is the captured URL
and/or the owner's note. A capture that arrived with a binary (image, PDF, any
file) additionally carries frontmatter pointing at its media.

Start every ingest with:

1. `git pull` — captures arrive as commits.
2. `bin/dex inbox` — mechanical reconcile: downloads any staged release assets
   into `media/<item-id>/`, git-adds them so LFS applies, deletes the remote
   assets, and rewrites each capture's frontmatter to
   `media: media/<item-id>/<file>`. Text captures pass through untouched. It
   needs GitHub auth (gh logged in, or GITHUB_TOKEN); if it reports missing
   auth or a failure, fix that with the owner before continuing — never work
   around it by hand-downloading. If it materialized anything, commit and push
   immediately — the remote asset is already deleted, so the repo copy is now
   the only copy.

Then process every capture with the one procedure below, and delete each
capture file when its item is done (the capture is preserved in git history;
its content lives on in the corpus).

Items handed directly in a session (a URL pasted in, an image or file dropped
in) follow the same procedure — no inbox involved. For session-provided media,
do what `bin/dex inbox` would have done: file it at `media/<item-id>/<name>`
(`item-id = sha1("media/<name>")[:6]`) and `git add` it so LFS applies, then
continue from step 1. If `media/<item-id>/` already exists for a *different*
file (generic names like `screenshot.png` collide), rename yours first —
prefix today's date — so ids stay one-to-one with files.

## Per capture

1. **Scope check** against CLAUDE.md's In-scope list — if the capture has
   `media:`, view the media first and judge its content. Unsure → ask the
   owner. Rejected → delete the capture file (and its `media/<item-id>/` dir
   if one was created) and say why.
2. **Corpus item.** The id:
   - Link/text capture: canonicalize the URL (strip utm/tracking params);
     `shortid = sha1("manual/<canonical-url>")[:6]`. Note-only captures with
     no URL: `sha1("manual/<capture-filename>")[:6]`.
   - Media capture: the item id is already fixed — it's the directory name
     `bin/dex inbox` created under `media/`.

   Write `corpus/YYYY/YYYY-MM-DD-<slug>-<shortid>.md` with frontmatter per
   `references/schema.md` (in this skill's directory): id, source (`inbox`|`manual`|exporter name),
   channel, shared_by, date, urls, kinds (media captures: `[image]` or
   `[file]`, plus a `media:` list pointing at the file(s)), `status: raw`,
   `enrichment: []`. Body = the capture's note verbatim — often the most
   valuable part.
3. **Enrich.** `bin/dex enrich run` (fetches captions, article text, READMEs,
   papers, tweets — and lead images/tweet photos into
   `enrichment/<id>/media-N.*` unless the instance config says otherwise).
   Caption-less video/audio: `bin/dex enrich whisper` (OPENAI_API_KEY in
   `.env`). For a media capture the primary source is the media itself: view
   it and write `enrichment/<id>/media-0.md` — what it depicts, all legible
   text (OCR), and, where relevant to this instance's domain, style, palette,
   composition, typography, layout. This is the media's "transcript"; make it
   substantive enough to stand in for the media in text-only contexts. PDFs
   and other files get the same treatment (read them directly).
4. **Harvest links.** Read the fetched content: substantive URLs found in it
   (post text, video description, transcript or caption mentions) join the
   corpus item's `urls` — then run `bin/dex enrich run` again so each becomes
   a first-class source under the same item. One hop from the share, never
   further: links found inside a *linked* page are not followed.
5. **Digest.** Read the enrichment fully — including viewing any media — and
   write `state/digests/<id>.md` per `references/state-formats.md`: frontmatter
   (id, date, `signal: high|medium|low`, topics, entities, and `media:` listing
   any media paths) + 3–15 standalone fact bullets with concrete specifics.
   Topics: use canonical names from `state/taxonomy.json` when it exists;
   otherwise 2–5 kebab-case candidates.
6. **Place.** Taxonomy exists → append the id to each matching topic's items.
   Create a new topic only once several items justify a page. When you do
   create one, sweep the existing digests (`state/digests/`) for items that
   belong to it — including `uncategorized-shares` — and move them in: a new
   topic usually reveals items that were previously overlooked or coarsely
   filed.
7. **Update the wiki.** Splice cited sentence(s) into affected pages —
   rewrite-not-append if "current state" changes. Pages citing a media item
   should usually embed it (relative image links). Update `wiki/index.md`;
   append a `wiki/log.md` line; delete the capture file.
8. **Commit + push**: `ingest: <short title>`.

## After the last capture

Check `wiki/log.md` for the most recent health check (a `| lint` entry). If
it is more than 7 days old — or there has never been one — run the dex-lint
skill now. Maintenance is the system's job, not the owner's; ingest is the
trigger that keeps it regular.

## Backfills (exports in raw/)

`bin/dex normalize` → scope-filter pass (judgment; purge via `bin/dex exclude
<file.json>`) → `bin/dex enrich run` → digest/place/assemble at scale. For 500+
items: work in waves; write work manifests BEFORE dispatching any parallel
agents; every agent contract includes "if an input is missing, STOP — do not
improvise"; verify coverage mechanically between waves.
