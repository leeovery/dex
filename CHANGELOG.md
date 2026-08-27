# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
