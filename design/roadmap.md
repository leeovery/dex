# dex — design queue

Queued design work and operational facts. The current design itself lives
where it's used: `CLAUDE.md` (structure + design in force), `docs/` (capture
protocol, shortcut recipe, backfills), and the skill references (schema,
state formats). This file holds what's *next*, not what is.

## Queued discussions (not yet designed)

- **Self-tuning cadence** — a scheduled run can reschedule its own task
  (`update_scheduled_task` is available to desktop scheduled sessions):
  hourly while captures flow, daily when quiet. Design once real run
  history exists.

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

- **Driver-based ingestion architecture** — the enricher should be an explicit
  driver registry per source kind. Existing drivers: youtube (captions +
  whisper), blog (trafilatura + wayback + og:image), github
  (repos/profiles/gists/issues), arxiv, tweet (fxtwitter + t.co follow +
  photos). To design: instagram, PDF, generic files, podcasts (audio fetch +
  whisper + show-notes links — today a Spotify/Apple link captures only
  the episode page), and a clean way to add more. Tweet driver: traverse threads — walking up the reply chain from a
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
