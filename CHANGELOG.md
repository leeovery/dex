# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.13] - 2026-09-01

✨ Added
- New `dex-map` command compiles a topic/entity/relation map into `state/map.json` and renders `wiki/index.md` from it, replacing the hand-maintained index.
- New `dex-enrich place` command writes taxonomy and entity-membership changes from a JSON payload instead of hand-edited files, validated whole and refused whole on any error.
- MCP server gains `topics`, `entities` and `graph` tools for reading the compiled map, plus `state/map.json` as an attached resource.
- Lint gains a placement advisory: flags digests naming a canonical topic the taxonomy doesn't record the item under.
- Migration 10 compiles `state/map.json` and `wiki/index.md` for existing instances at sync time.

🔧 Changed
- Lint's index checks (pages missing from index, ghost index entries) are replaced by a single map/index freshness check — a byte diff against a recompile.
- `dex-serve`'s `fetch` and `page` tools now return only page/digest bodies, never frontmatter, and `fetch` surfaces a digest's `signal` separately from its facts.
- `dex-serve` search snippets and hits no longer match against digest frontmatter, only the fact body.
- The MCP `graph` tool's default view is capped to keep responses readable, with `around`, `min_weight` and `full` to widen it.

🐛 Fixed
- `dex-exclude` now recompiles the map and index after purging items, so stale entries no longer linger in derived artifacts.

## [0.1.12] - 2026-08-31

🔧 Changed
- Instagram carousels and X threads now pool up to 100 media files instead of truncating at 4, since those files are the post itself, not embedded extras.
- Extraction assets (embedded images) keep the tight 4-file cap unchanged — only carousel/thread downloads got the pooled bound.

🐛 Fixed
- A one-time migration reseeds Instagram/X media that the old 4-file cap wrongly refused on existing instances, and re-walks truncated Instagram posts to discover slides that were never even fetched.

## [0.1.11] - 2026-08-31

✨ Added
- Connect Claude Code to your dexes, not just the desktop app — `dex-connect --client code` files the server at user level so every Claude Code session on the machine can search and capture into your dexes.
- A page URL whose body turns out to be an image, SVG, or other picture now re-detects to the media stage instead of being extracted as garbage text.
- Sync's report now flags a connected server whose launcher no longer exists on disk ("broken"), separately for each chat client.

🔧 Changed
- `dex-connect` now writes every chat client found on the machine by default, skipping (and reporting) any that aren't installed rather than failing outright.
- Sync's chat-connection gap is now reported per client instead of a single combined line, so being connected in one client no longer hides a gap in another.
- `docs/connect.md` and `docs/start.md` walk through connecting both the desktop app and Claude Code, asking about each one separately.

🐛 Fixed
- A stated media path that names no file on disk now parks as a manual item instead of vanishing silently or being miscounted as a pending description.
- MPEG sync-frame detection no longer misidentifies a UTF-16 page body as an MP3 when deciding whether a body IS media.

## [0.1.10] - 2026-08-30

✨ Added
- `dex-serve` and `dex-connect`: reach dex from the Claude desktop app's chat — ask questions, get answers with citations, and save links or notes into an instance's inbox, all without opening a folder session.
- `dex-serve` exposes `search`, `fetch`, `page`, and `capture` tools plus a `dex-query` prompt over MCP, tagging every result with its source instance.
- `dex-connect` merges a `dex` server entry into the desktop app's config in one command, launched through an anchor instance's own `bin/dex` so its engine pin stays the version authority.
- Sync now notices when a machine's desktop app has no dex connected (or is missing one instance) and adds a "Chat connection" line to the report, prompting the session to offer the setup.
- `docs/connect.md` walks a session through the whole connection setup end to end, and `docs/start.md` now offers it as Step 7 of onboarding a new instance.
- Backfill guidance for pulling a Discord export via DiscordChatExporter, including token handling and channel discovery.

🔧 Changed
- Corpus, wiki, inbox, and taxonomy paths on `Instance` are now named properties (`inbox_dir`, `wiki_dir`, `taxonomy_path`) instead of ad hoc path joins.
- The bundled instance template is now resolved through one shared `template.py` helper instead of being duplicated across `sync.py` and `new.py`.

## [0.1.9] - 2026-08-30

✨ Added
- Undescribed media are now tracked: any item carrying more media files (or attached markdown/documents) than written descriptions shows up on the run report, `enrich status`, and the health check under a new "Describe these" queue until the descriptions are written.
- Backfill attachments are now materialized into `media/` automatically — a migration copies existing exports' local attachments out of `raw/` so they finally enter extraction, digests, and the describe queue instead of being silently invisible to the pipeline.
- Parked units waiting on a capability with no mechanical provider (the "cognitive floor") are now distinguished from ones genuinely waiting on an active provider, both in the ledger and on the run/status reports — captioned "no provider will appear — you read this one" instead of a misleading retry note.

🔧 Changed
- `raw/` exports are now tracked by Git LFS wholesale instead of by a fixed extension list, so exporter-named assets with unusual or missing extensions no longer slip past LFS tracking.

## [0.1.8] - 2026-08-28

🔧 Changed
- The closing report to the owner at the end of a run is now composed with judgment rather than rendered from a fixed surface — its required content is still fixed, but its shape adapts to what happened.
- Item receipts render through the surface as before in attended sessions, but in scheduled runs they now stay in the transcript and are summarized in the closing report instead of being emitted verbatim.

## [0.1.7] - 2026-08-28

✨ Added
- A parked run that waits on transcription now re-queues and drains in the same run instead of needing a second run — a media reel goes from fetch to transcript in one pass when a provider is active.
- The status report marks a waiting item as drainable when its provider is active, instead of wrongly telling you it's waiting for one to appear.

🔧 Changed
- `enrich pass` now rejects an item id that doesn't match a live corpus item, catching accidental multi-line pastes of ids that used to silently record as one unclaimed, unresolvable entry.
- A rerun that returns a much smaller body (e.g. a paywall or login stub) now keeps the previously stored content instead of overwriting it with the stub.
- Dropping a unit's superseded output file on re-landing now matches any file recording the same URL, not just files following the old naming pattern — so hand-renamed files get cleaned up too.

🐛 Fixed
- Migration added to remove leftover duplicate output files that a past defect left behind, once per affected item.
- Migration added to split old pasted-list pass records into correct per-item records.
- Migration added to restore article bodies that were previously overwritten by a truncated wayback-archive rescue, using the instance's own git history.

## [0.1.6] - 2026-08-27

✨ Added
- Extraction lifts a page's declared description (`og:description` or `description` meta) into the frontmatter — recovers standfirsts/decks that trafilatura drops as header boilerplate, like Substack's post-header sentence.
- Downloaded media is byte-sniffed and named by what it actually is, not the URL's extension — catches CDNs that content-negotiate a different format than the URL claims (JPEG served from a `.png` URL, AVIF/HEIC/HEIF/MP4/MOV containers, WebM vs MKV, SVG).
- A media URL that answers with an HTML/XML/JSON page instead of real media bytes is now blocked (and escalates to manual after repeated attempts) instead of silently saving the page as an image file.
- `dex-lint` gained a `media:` drift advisory — flags a digest whose stated media paths no longer match what's actually on disk, in either direction, without failing the check.
- Digests now list every media file an item carries, including files the media stage downloaded into `enrichment/<id>/` that never made it into the item's own frontmatter.

🐛 Fixed
- A heading's permalink icon rendered as a visible glyph character (`#`, `¶`, `§`) no longer swallows the whole heading or leaks the glyph into the heading text.
- A redownloaded media file that changes format between runs (e.g. a CDN switching from JPEG to PNG) now replaces the old file in its slot instead of leaving both on disk, double-spending the per-item media cap.
- An interrupted download's leftover temp file is no longer miscounted as a real media file against the per-item cap, and gets swept up when its slot is next written.

## [0.1.5] - 2026-08-27

✨ Added
- New Instagram driver — share a post link and get its caption, author, date and media, read without needing an account; videos are transcribed, images kept beside the caption, and private posts park with a note to screenshot instead.
- `instagram_base_url` config key to point at an alternate media proxy if the default host goes down.

🔧 Changed
- Media downloads that come back empty now retry instead of being recorded as done with a blank file.
- Provider errors on Instagram parks (e.g. no speech detected) now say the caption is already stored, so it can be kept as the record instead of chased as a failure.
- A dead media-proxy URL for an Instagram post now asks for a requeue or a different proxy host, rather than being treated as permanently gone.

## [0.1.4] - 2026-08-27

🐛 Fixed
- A `<br>` between two parts of an HTML heading (e.g. a title and its subtitle) now becomes a space instead of being dropped, so the parts no longer get welded into one run-together word.

## [0.1.3] - 2026-08-27

🔧 Changed
- Removed CI as a separate workflow — the four gates (tests, ruff, ty, format) now run only locally and at release time, not on every push or PR.

## [0.1.2] - 2026-08-26

🔧 Changed
- CI now runs on pushes to every branch, not just pull requests.
- The release tag build now bumps `pyproject.toml` and `uv.lock` to the release version before building the wheel, keeping the tag's lockfile in sync with its version.

## [0.1.1] - 2026-08-26

✨ Added

- `DEX_ISSUE_REPO` environment variable — point crash/issue filing at a scratch tracker instead of the shared public one, useful while testing the engine itself.
- Run reports now surface an "Already stored" section when a re-fetch matches what's already on disk, so a run no longer says "nothing to report" right above a count of units it just processed.

🔧 Changed

- X posts with a long-form article now render the article's full body (headings, lists, quotes) instead of just the announcement text, which previously stored only a link and threw the article away.
- Sharing an X article URL directly now parks with guidance to capture the original post instead, since the article can't be fetched on its own.
- Web page extraction now repairs known markup traps before parsing — empty permalink anchors that were swallowing heading text and code blocks, and line-break-padded headings — and automatically keeps a discussion thread's comments when they clearly are the content, rather than always stripping comments.

🐛 Fixed

- Crash-report scrubbing now also catches URLs with the scheme stripped off and bare item-id slugs, closing two ways private addresses or item identifiers could leak into a filed issue.
- A ledger migration requeues X posts that were previously stored as a bare article link so their real content gets re-fetched.

## [0.1.0] - 2026-08-25

✨ Added

- Driver-based ingestion for web pages, X posts, YouTube, GitHub, arXiv papers, podcasts, and documents — each source is fetched by the driver that understands its shape.
- Transcription, document extraction, and OCR as pluggable capabilities — audio gains transcripts, and PDF/Word/PowerPoint files become readable markdown.
- Link harvesting via `bin/dex enrich fetch` — a session promotes the links worth following from a fetched page, bounded by depth and a per-item URL budget.
- Media capture — a page's lead images and a post's attachments download beside its text, LFS-tracked whatever the extension.
- Engine-defect reporting via `bin/dex issue` and an automatic crash filer — issues reach the public tracker with nothing private in the payload.
- Rendered report surfaces via `bin/dex render` — run reports, status, receipts, and health checks are consistent markdown, never hand-drawn.
- Verb-written state throughout — digests, heals, stage passes, exclusions, and compaction each have a command, so a malformed record is impossible.
- Health checks with judgment repairs — lint names every finding with its repair, and sessions reconcile counts against the work they just did.
- Tag-pinned releases — `bin/dex sync` bumps an instance's pin, refreshes machinery, and runs migrations in one motion.
- Three migrations that carry a v0.0.1 instance to this engine losslessly — rehearsed on faithful copies of five real instances before tagging.
- `dex-new` instance scaffolding and a single getting-started guide covering dependencies, create-or-join, and scheduling.
- Phone-shortcut capture of images, PDFs, and media alongside links and text.
- CI across Python 3.11-3.13 on Linux and macOS, a daily third-party drift watch, and a pre-tag release gate.

🔧 Changed

- Source kinds are renamed — tweets are `x` and blogs are `web`, with existing state and files migrated automatically.
- `normalize-config.json` becomes `state/config.json` — unknown keys are rejected loudly instead of ignored.
- X, YouTube, and arXiv links collapse to one canonical identity per work, so duplicate spellings no longer fetch twice.
- Run reports name every item owing work and the exact command that heals anything parked.

🗑️ Removed

- The `dex-ingest` skill is retired — capture and run each own their half of it.
- The never-applied `name_map` config key is gone.

🐛 Fixed

- A site blocking the fetcher now parks as `blocked` and retries with backoff instead of being marked dead forever.

## [0.0.1] - 2026-08-18

Initial release.
