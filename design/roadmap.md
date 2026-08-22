# dex — design queue

Queued design work and operational facts. The current design itself lives
where it's used: `CLAUDE.md` (structure + design in force), `docs/` (capture
protocol, shortcut recipe, backfills), and the skill references (schema,
state formats). This file holds what's *next*, not what is.

## Bugs

- **Pre-rewrite `dead` ledger entries need healing, per instance** — the old
  enricher could not tell a blocked fetch from a gone one, so transient blocks
  were ledgered `dead`, a terminal status that is never retried. The engine no
  longer does this (403/429/5xx → `blocked`, retried every run; `dead` reserved
  for 404/410/NXDOMAIN), but migration 2 seeds only `done` entries — verdicts
  the old code condemned are not reseeded. Known case: a business site in one
  production instance (2026-08-19), hand-healed into `enrichment/` while the ledger
  still says `dead`, so healed state and ledger disagree. Close these out with
  `bin/dex enrich mark` during each instance's post-merge sync review.

## Queued discussions (not yet designed)

- **Self-tuning cadence** — a scheduled run can reschedule its own task
  (`update_scheduled_task` is available to desktop scheduled sessions):
  hourly while captures flow, daily when quiet. Precondition met
  2026-08-20: first real run history exists (one instance, 17 hourly
  runs — work clustered 20:05/23:05/09:05, nine consecutive overnight
  no-ops; owner interest confirmed). Shape: simple backoff with hard
  bounds — tighten after working runs, stretch after consecutive no-ops,
  snap tight when captures arrive; report each reschedule with its reason.
  Mostly a dex-run skill rule; queue behind the pipeline rewrite and the
  watchers design.

- **App-only owner surfaces** — for an owner who lives in the Claude app,
  two query-surface candidates to test: a Cowork session on the instance
  folder (the sandbox's egress proxy allows github.com, so pull-first
  freshness may work over an HTTPS remote with a token), or a claude.ai
  Project with `wiki/` synced via the GitHub integration (read-only,
  manual sync, token-capped). The local scheduled task keeps the folder
  fresh either way. Cloud runners (Claude Code routines, Cowork cloud
  tasks, Managed Agents) remain the path for owners with no computer —
  they need a clone-first run mode and a stored per-instance PAT; not
  built, not needed yet.

- **Ingestion pipeline — open ends** — the ingestion design in force is
  `design/ingestion-pipeline.md`. Open here: X walk-down, and engine OCR
  providers (the cognitive floor is the only OCR path). The instagram
  driver and the hosted-transcription pick are their own entries below.

- **Code cleanup round (post-first-release)** — behavior-neutral debts in
  the engine, batched so they aren't lost: extract one shared
  fetch-and-classify helper (eight hand-rolled copies across
  drivers/transcribe/run); dedupe driver body/slug helpers (youtube
  `_body` vs `transcribe.youtube_body`; capture vs normalize
  `URL_RE`/slugify; `run._ext_of` vs `transcribe._audio_ext`; six
  spellings of Classification→Result); one enrichment-frontmatter module
  owning render+parse (writer in run.py, reader in transcribe.py); hoist
  the yt-dlp seam out of drivers.youtube so pipeline.transcribe stops
  importing a driver; ledger.py's per-word legacy-vocabulary hint tables; registry's
  module-level DRIVERS vs the zero-import-time-state rule; streaming
  transport reads (unbounded `response.read()` buffers whole enclosures —
  enforce the media cap in-stream); a per-item fetched-count cache
  (`_cap_fired` is quadratic); one long-lived append handle for the
  ledger; a repo-wide `ruff format` pass (43 files drift under ruff
  0.16.3 — one mechanical reformat commit); a cheap guard in
  `run._admit_children` (unreachable until a driver emits children).

- **Per-instance context instructions (beyond scope)** — some instances need
  more than a scope list: standing context that steers scanning, enrichment,
  and digestion (e.g. which link shapes are noise here, what the community's
  shorthand means, what depth a domain deserves). To design: a designated
  content file the skills consult when present (not README — that's
  human-facing and already multi-purpose), offered as an option during
  setup/ingest, shipped as an empty slot in the template. Hard constraint:
  content only — machinery stays identical across instances; most instances
  never fill it.

- **Source removal** — no story yet for removing an entire source after
  ingestion (a Discord channel that turns noisy, an exporter that was a
  mistake). Exists today at item level only (`bin/dex exclude`) and URL
  level (`internal_domains`). To design: bulk exclusion by source/channel,
  what happens to digests, citations, and pages that lean on removed items,
  and whether raw/ keeps or drops the export archive.

- **Instagram driver** — shape not agreed. Facts: no official API for
  arbitrary public posts (Graph API = own business accounts only). Options
  mapped: instaloader + dedicated session (residential IP only, flagging
  risk), paid scraper APIs (Apify etc., ~$/1k posts, run anywhere),
  screenshot-capture fallback (works today). Requirements when built:
  caption, full carousels, likes/follower counts; videos = metadata + link
  only.
- **Resurfacing and the owner's reading queue** — the corpus tracks machine
  ingestion but not owner engagement: saved things vanish from mind exactly
  like bookmarks and screenshot folders did. To design: (1) an owner
  read/intent flag per item as derived state — fed by the capture note
  ("read later", "try on project X") and a presumed-unread default for
  substantive items, cleared conversationally; (2) resurfacing views over it:
  index leads with "new this week" / "waiting for you", query answers end
  with related-but-unread items, and a periodic digest page composed by the
  scheduled session (rides the scheduled-ingestion design). Recall queries
  ("what did I share about X", "what came in last month") answer from
  corpus+digests — make that an explicit mode in dex-query. Principle: one
  store (the corpus); queue, digest, and index sections are regenerable
  views. A TUI/newsletter delivery layer waits until the digest proves what
  the owner actually wants to see.
- **Watchers — feeding sources as a first-class layer** — preliminary
  discussion logged in `design/watchers-discussion.md` (2026-08-20): every
  feeder (shortcut, Discord, RSS, bookmark folders, Dropbox) becomes a
  watcher writing standard inbox captures through one shared inserter;
  cursor position controls history-vs-forward scope; raw/ likely dies into
  namespaced watcher scratch; staleness/availability become per-watcher
  facts the report states. Supersedes the "backfill-source freshness"
  idea (the 2026-08-20 overnight analysis found Discord blind for two
  days, discoverable only by deep analysis). Design it AFTER the
  ingestion-pipeline rewrite lands — it sits entirely upstream of that
  design and requires no changes to it.

- **Offline instances can't run bin/dex at all** — measured during the
  pipeline rewrite (2026-08-20, uv 0.12.5): uvx re-resolves the git ref on
  every invocation (good: pin bumps take effect immediately, no cache
  staleness) but fails hard when the source is unreachable, even with a
  fully cached env. A laptop on a plane can't lint or capture-materialize.
  To consider: a shim fallback to the cached env when resolution fails,
  or `uv tool install` as an offline-capable channel.

- **Hosted transcription providers (Groq et al)** — the transcribe capability
  ships whisper-local (default) plus an OpenAI-compatible whisper-api provider
  (base_url-configurable; ffmpeg chunking removes upload limits, so any
  compatible host works today). To investigate for a documented recommended
  config: Groq whisper-large-v3-turbo, Fireworks, Deepgram (URL ingestion, no
  upload), Cloudflare Workers AI whisper — compare price per audio hour,
  accuracy vs local medium, rate limits.

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
- Desktop scheduled tasks (Claude app, Code tab → Routines → Local) run as
  real local Claude Code sessions: full user PATH, gh auth, git-lfs, uv,
  unrestricted network. File-backed at
  `~/.claude/scheduled-tasks/<name>/SKILL.md` (frontmatter + prompt body;
  schedule/folder/model live outside the file). Presets
  manual/hourly/daily/weekdays/weekly; finer cron by asking a desktop
  session. Fire only while the app is open and the machine awake; one
  catch-up run on wake covers the most recent miss (7-day window).
- Cowork scheduled tasks are cloud unless a local folder is declared at
  creation (can't be added later). "Run on your computer" there means an
  Ubuntu aarch64 VM with the folder mounted at the identical path: egress
  proxy allows github.com only (not raw.githubusercontent.com or any other
  subdomain), git/uv/ffmpeg/python present, no gh, no git-lfs, no root.
  Git over HTTPS to github.com works; enrichment fetches and the
  release-asset API are blocked.
- Cowork cloud containers have full network and git/git-lfs/uv/ffmpeg —
  the engine builds and runs there via uvx — but no gh; auth would need a
  stored per-instance PAT.
