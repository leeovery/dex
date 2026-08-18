# dex — design queue

Queued design work and operational facts. The current design itself lives
where it's used: `CLAUDE.md` (structure + design in force), `docs/` (capture
protocol, shortcut recipe, backfills), and the skill references (schema,
state formats). This file holds what's *next*, not what is.

## Queued discussions (not yet designed)

- **Driver-based ingestion architecture** — the enricher should be an explicit
  driver registry per source kind. Existing drivers: youtube (captions +
  whisper), blog (trafilatura + wayback + og:image), github
  (repos/profiles/gists/issues), arxiv, tweet (fxtwitter + t.co follow +
  photos). To design: instagram, PDF, generic files, and a clean way to add
  more. This is the core value of the system — solve it properly.
- **Instagram driver** — shape not agreed. Facts: no official API for
  arbitrary public posts (Graph API = own business accounts only). Options
  mapped: instaloader + dedicated session (residential IP only, flagging
  risk), paid scraper APIs (Apify etc., ~$/1k posts, run anywhere),
  screenshot-capture fallback (works today). Requirements when built:
  caption, full carousels, likes/follower counts; videos = metadata + link
  only.
- **Paid media/object storage** — S3 or Cloudflare R2 as a media store
  replacing/augmenting LFS (LFS free tier = 1GB), and/or as capture staging.
  Storage is one substitutable ingest step — capture clients and pointers are
  unaffected. Worth deciding only once an image-heavy instance shows real
  volume; note the access-control trade: LFS keeps media behind the repo's
  own permissions, a public bucket does not.

## Operational facts worth remembering

- Shared iCloud shortcut links are frozen snapshots: edits never propagate;
  each re-share mints a new URL; importers must reinstall and re-answer import
  questions. Fields with import questions are auto-cleared in the shared copy.
- Shortcuts' Base64 Encode must have Line Breaks = None (GitHub rejects
  wrapped base64).
- GitHub returns 404-shaped JSON for repos a token can't reach — Shortcuts
  shows a checkmark; silent failure.
- Instance repos owned by orgs can't use no-expiration fine-grained PATs;
  classic PAT (repo scope) is the workaround.
- Shortcuts variable chips can go stale after nearby edits: the chip renders
  normally but resolves empty at run time. Symptom: "Cannot parse response"
  on a request whose config looks correct. Fix: delete and re-insert the
  chips.
- Get Images from Input dereferences URL inputs (fetches them); the X app's
  share URL redirects through a non-HTTPS scheme and errors. Links must be
  detected and diverted before any Get Images call.
