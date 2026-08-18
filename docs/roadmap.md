# dex — decisions and queued design work

Living document. Decisions here are agreed with Lee; queued items need
discussion before building.

## Agreed, to build next

### Release-asset capture staging (binary captures done properly)

Problem: API uploads bypass LFS, so image/file captures land as raw blobs in
git history (permanent bloat); the alternative "true LFS via API" dance leaves
orphaned LFS objects in billed storage forever (GitHub exposes no remote prune).

Agreed shape — each layer used for its design intent:

1. Capture client uploads the binary as a **release asset** on a standing
   "inbox" release (raw binary body — Shortcuts' File request body; no base64,
   one request), then PUTs a tiny `.md` pointer into `state/inbox/` carrying
   the asset URL + the note.
2. Ingest downloads the asset, files it at `media/<item-id>/` via a **local
   git add** (LFS applies canonically), then **deletes the release asset**.
3. End state: media in LFS as designed, git history text-only, nothing
   orphaned.

Build items: engine (scaffold creates the standing release; ingest skill
update), shortcut doc rewrite of the image/file branches (asset upload +
pointer), then re-share the shortcut (links are frozen snapshots — see below)
and pin the new link. Land BEFORE Natasha installs and BEFORE dex-design.

## Queued discussions (not yet designed)

- **Driver-based ingestion architecture** — the enricher should be an explicit
  driver registry per source kind. Existing drivers: youtube (captions+whisper),
  blog (trafilatura+wayback+og:image), github (repos/profiles/gists/issues),
  arxiv, tweet (fxtwitter + t.co follow + photos). To design: instagram, PDF,
  generic files, and a clean way to add more. This is the core value of the
  system — solve it properly.
- **Instagram ingestion** — HELD by Lee; shape not agreed. Facts: no official
  API for arbitrary public posts (Graph API = own business accounts only).
  Options mapped: instaloader + dedicated burner session (residential IP only,
  flagging risk), paid scraper APIs (Apify etc., ~$/1k posts, run anywhere),
  screenshot-capture fallback (works today). Requirements when built: caption,
  full carousels, likes/follower counts; videos = metadata + link only for now.
- **Paid media/object storage** — Lee is open to paying small amounts to run
  this properly: S3 or Cloudflare R2 (he has a Cloudflare account). Candidate
  roles: media store replacing/augmenting LFS (LFS free tier = 1GB), and/or
  capture staging. Discuss vs release-assets + LFS once dex-design's real
  volume is known.
- **dex-design** — the 4th instance, image-heavy. Blocks on release-asset
  staging; will exercise the image pipeline (vision descriptions as image
  "transcripts", gallery-style wiki pages).

## Operational facts worth remembering

- Shared iCloud shortcut links are frozen snapshots: edits never propagate;
  each re-share mints a new URL; importers must reinstall and re-answer import
  questions. Fields with import questions are auto-cleared in the shared copy.
- Shortcuts' Base64 Encode must have Line Breaks = None (GitHub rejects
  wrapped base64).
- GitHub returns 404-shaped JSON for repos a token can't reach — Shortcuts
  shows a checkmark; silent failure. (Bit us twice: PAT scope, dictionary row.)
- Instance repos owned by orgs can't use no-expiration fine-grained PATs;
  classic PAT (repo scope) is the workaround (dex-curated/Natasha).

## Pending on Lee's device

- Fix the Marketing dictionary row (marketing captures currently 404 silently).
- Rebuild shortcut per current doc: resize step (2048w) + generic file branch.
- After release-asset staging lands: final rebuild, re-share, pin new link in
  docs/shortcut.md.
