<div align="center">

# 🧠 dex

**Your own knowledge base, maintained for you by Claude.**

Save things with one tap. Ask questions later. Everything cited, dated, and kept current.

[![Runs on Claude](https://img.shields.io/badge/runs%20on-Claude-cc785c)](https://claude.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://github.com/leeovery/dex/blob/main/pyproject.toml)
[![Powered by uv](https://img.shields.io/badge/powered%20by-uv-261230)](https://docs.astral.sh/uv/)

</div>

---

You keep finding good things — videos, articles, papers, repos, threads. They pile up
in bookmarks and group chats and disappear. **dex** turns that stream into a living
wiki: every source fetched and transcribed, every fact traced to where it came from,
every page kept current as new material arrives — with the old understanding preserved
as history, not overwritten.

You don't run anything. Claude operates the whole system; your job is taste —
save things, ask questions, correct it when it's wrong. The design follows Karpathy's
[llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern:
*"The human's job is to curate sources, direct the analysis, ask good questions.
The LLM's job is everything else."*

## Getting started

Let Claude set it up for you. The Claude desktop app is the right home for
it — a scheduled task there sweeps your captures on its own, and any
session on the folder answers questions. The Claude Code CLI works just as
well. Start a session on your home folder (a session has to start somewhere;
setup picks the dex's real home with you) and paste:

```
Fetch https://raw.githubusercontent.com/leeovery/dex/main/docs/start.md and follow it.
```

Claude asks whether you're creating a new dex or joining an existing one,
interviews you, and sets everything up. Prefer to drive it yourself? Open
[docs/start.md](docs/start.md) and follow it.

## Living with it

- **Save from anywhere** — share a link from your phone or paste it into a session:
  "add this to dex". It gets fetched, transcribed if it's a video, filed with
  provenance, and woven into the relevant pages.
- **Ask anything** — "what's the current thinking on X?" Claude answers from your
  wiki with citations, prefers the newest material, and files genuinely new answers
  back in so they compound.
- **Trust but verify** — the knowledge base checks itself over: when an
  ingest finds no health check in the past week, it runs one — broken links,
  missing citations, stale pages, contradictions, and overgrown topics get
  found and fixed. You can also ask for one anytime. Corrections you make by
  hand are pinned and survive every rebuild.

One **dex**, many brains: each knowledge base is its own private *instance* repo
(dex-cooking, dex-woodworking, one for your partner's business...) and this public
repo is the shared engine they all run on.

## How it works

```
sources (phone captures, chat exports, links dropped in a session)
  ↓ normalize          one corpus item per share, provenance stamped
  ↓ enrich             everything behind every link: transcripts, articles, READMEs, papers
  ↓ digest    (Claude) a permanent fact-index of every item
  ↓ taxonomy  (Claude) topics + entities, discovered bottom-up
  ↓ assemble  (Claude) wiki pages — dated claims, [[wikilinks]], citations
  ↺ verify + lint      claims checked against sources; full coverage enforced
```

What makes it trustworthy rather than vibes-in-a-wiki:

- **Provenance at ingest** — who shared it, where, when: captured once, at the only
  moment it exists.
- **The wiki is a build artifact** — the corpus is truth; pages regenerate without
  losing anything.
- **Every claim cites corpus items** (never other wiki pages), and citations are
  mechanically checked.
- **Coverage is enforced** — every item is cited somewhere or explicitly ledgered.
  Nothing silently vanishes.
- **Recency first** — pages lead with "current state (as of ...)" and keep superseded
  practice as marked history. Conflicts between old and new are surfaced, not
  smoothed over.

See `example/` for a three-file toy instance showing the shapes, and
`instance/skills/dex-run/references/schema.md` for the corpus format.

<details>
<summary><b>Under the hood</b> (for agents and the curious — humans never need this)</summary>

Instances run the engine's mechanical commands via a `bin/dex` shim
(`uvx --from git+https://github.com/leeovery/dex@<pinned-tag> dex-<cmd>`,
cwd = instance root; the tag lives in `.dex-engine-pin`, bumped by sync):

| command | does |
|---|---|
| `dex-enrich run` | drain the ledger work queue: fetch behind every corpus URL and file — captions, articles (Wayback fallback), GitHub, papers, X thread walk-up, podcast audio → transcript, document extraction — and report the session's cognitive work list |
| `dex-enrich status` | ledger summary, waiting cohorts, interrupted-session backstop, capability report |
| `dex-enrich transcribe` | drain the waiting transcription cohort (whisper local floor / OpenAI-compatible API; `--limit`, `--model`) |
| `dex-enrich fetch` | fetch extra URLs into an existing item, ledgered as children (the harvest verb) |
| `dex-enrich compact` | rewrite the ledger to the latest line per unit (settles union merges) |
| `dex-enrich mark` | heal one ledger entry — the sanctioned correction verb |
| `dex-enrich pass` | record a stage completion (harvest/digest/wiki) in `state/passes.jsonl` |
| `dex-enrich item new` | create a corpus item from a capture file (id rules + provenance; code writes frontmatter) |
| `dex-normalize` | raw chat exports → corpus items (DiscordChatExporter JSON) |
| `dex-lint` | mechanical health check: wikilinks, citations (shortid flags included), orphans, index drift, stale pages, count drift, restated-fact warnings, ledger schema, quarantine, pass records (`--write` reconciles derived wiki frontmatter) |
| `dex-exclude <json>` | permanently purge out-of-scope items (survives re-normalization) |
| `dex-inbox` | materialize staged binary captures: release asset → `media/<id>/` (LFS), asset deleted (`ensure` creates the standing inbox release) |
| `dex-sync` | pin check + engine upgrade, migrations, machinery refresh, sync report — step 0 of every run |
| `dex-render` | render a named report surface from a JSON payload, verbatim (skills never hand-draw tables) |
| `dex-new <name>` | scaffold a new instance from the engine's bundled template |

The pipeline is ledger-driven (`state/enrichment-ledger.jsonl` is the work
queue, append-only, last-line-per-unit): drivers per source shape, one
central failure classifier (blocked ≠ dead, ever), capabilities with a free
local floor (faster-whisper transcription; document extraction), and an
issue filer that reports engine bugs upstream — sanitized by construction —
so every instance heals through releases and sync.

Capture inbox: every capture is one `.md` in `inbox/`, written via the
GitHub contents API (the phone shortcut) or committed directly by the
dex-capture skill — body = URL and/or note; a capture that carried a binary
(image, PDF, any file) stages it as an asset on the repo's standing `inbox`
release and references it in frontmatter. No server-side machinery at all: the
PUT is the commit, and the next run moves staged binaries into `media/` where
LFS applies. Suggestion is untrusted by design — scope filtering happens at
processing time, inside the instance. Full protocol: `docs/capture.md`.

Instance layout: `CLAUDE.md` (scope + operations contract) · `inbox/` (pending
captures) · `raw/` (verbatim exports) · `corpus/` (append-only items) ·
`enrichment/` · `media/` (captured binaries, LFS) · `wiki/`
(topics/entities/syntheses plus index, log, pins) · `state/` (digests,
taxonomy, the ledger and other append-only JSONL, config.json) · `cache/`
(gitignored ephemera) · `.dex-engine-pin` (the engine release this instance
runs).

</details>
