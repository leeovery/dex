# dex — design queue

Queued design work and operational facts. The current design itself lives
where it's used: `CLAUDE.md` (structure + design in force), `docs/` (capture
protocol, shortcut recipe, backfills), and the skill references (schema,
state formats). This file holds what's *next*, not what is.

## Queued discussions (not yet designed)

- **Scheduled ingestion** — captures land in `inbox/` but nothing ingests
  them until a session runs, so the wiki drifts stale between sessions. To
  design: how ingest runs on a regular schedule. Candidates: Claude Cowork
  scheduled sessions (first to test), headless Claude Code on a cron/launchd
  timer, a scheduled cloud session. Decide alongside: schedule vs
  capture-triggered, and how failures surface — ingest is a judgment
  operation (scope checks, digests, wiki edits), not a blind pipeline, so
  whatever runs it must be a full Claude session operating the instance
  contract. The same schedule carries the health check (today it rides a
  7-day staleness trigger at the end of ingest).

- **Driver-based ingestion architecture** — the enricher should be an explicit
  driver registry per source kind. Existing drivers: youtube (captions +
  whisper), blog (trafilatura + wayback + og:image), github
  (repos/profiles/gists/issues), arxiv, tweet (fxtwitter + t.co follow +
  photos). To design: instagram, PDF, generic files, and a clean way to add
  more. Tweet driver: traverse threads — walking up the reply chain from a
  shared post is cheap (each post names its parent); walking down from a
  thread's first post needs design. Engagement counts stay unrecorded
  (snapshot noise) except where a driver's requirements say otherwise. Also enrichment depth: the blog driver fetches only the shared URL,
  so product sites that spread substance across landing/pricing/docs come in
  thin — consider a site driver that pulls a small judged set of pages
  (capped, never a crawler). This is the core value of the system — solve it
  properly.
- **Instagram driver** — shape not agreed. Facts: no official API for
  arbitrary public posts (Graph API = own business accounts only). Options
  mapped: instaloader + dedicated session (residential IP only, flagging
  risk), paid scraper APIs (Apify etc., ~$/1k posts, run anywhere),
  screenshot-capture fallback (works today). Requirements when built:
  caption, full carousels, likes/follower counts; videos = metadata + link
  only.
- **Update channel: main vs pinned releases** — instances track the engine's
  `main` on every `bin/dex` run (uvx from git), and synced machinery follows
  via the weekly self-triggered health check. No staged rollout: a bad push
  reaches every instance at its next command run. Option: the shim pins a
  release tag and `dex-sync` bumps the pin, making mint releases the actual
  distribution channel. Trade-off: deliberate rollout vs the freshness the
  current single-maintainer loop relies on.
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
