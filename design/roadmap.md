# dex — decisions and queued design work

Living document. Decisions here are agreed with Lee; queued items need
discussion before building.

## Built: release-asset capture staging (2026-08-18)

Binary captures no longer enter git history as raw blobs. The unified design
("one method for receiving content" — Lee):

- **Every capture is one `.md` in `inbox/`** — body = URL and/or note
  (the note always lives in the body now; the note-as-commit-message hack is
  gone). A capture that carried a binary adds frontmatter: `asset:` (API URL
  of a release asset on the repo's standing `inbox` release) + `name:`.
- **Shortcut**: branches only prepare variables (`blob`/`ext`, then `payload`);
  binaries upload as a raw File body (no base64) to the inbox release; one
  shared final PUT of `{stamp}.md` for every capture kind. Rewritten in
  `docs/shortcut.md` — needs the on-device rebuild.
- **Engine**: `dex-inbox` reconciles the inbox (asset → `media/<id>/` +
  git add so LFS applies — verified staged as an LFS pointer before the asset
  is deleted; pointer frontmatter rewritten to `media:`); `dex inbox ensure`
  creates the standing release (also self-heals on each run); orphaned assets
  (upload succeeded, pointer PUT failed) are reported. Auth: GITHUB_TOKEN →
  `gh auth token` — **gh is a declared instance dependency** (checked at
  setup); no PAT is ever stored in an instance. The only stored token is the
  one inside the shortcut (one field, all instances).
- Ingest skill is one per-capture procedure; media is just another enrichment
  source ("transcript" via description/OCR).

Still to land before Natasha installs / dex-design: on-device shortcut
rebuild per the new doc, re-share, pin the link.

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
- Rebuild shortcut per the rewritten docs/shortcut.md (release-asset staging:
  Step 5 restructured — shared final PUT, binary asset upload, pointer file).
  Two spots to device-verify during the build: the asset POST's Request Body
  **File** + `Content-Type: application/octet-stream` header, and repointing
  an If's input to the `blob` variable.
- Then re-share, pin the new link in docs/shortcut.md.
