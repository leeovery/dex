# dex

**An LLM-maintained personal knowledge base, done as files.** You curate sources —
links, videos, papers, chat exports, things you saw on your phone — and an LLM agent
(Claude Code, Cowork, or any agent that operates on a repo) compiles them into a
living wiki: provenance-stamped, citation-anchored, recency-aware, and regenerable.

The pattern is Karpathy's [llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
idea taken to production: *"The human's job is to curate sources, direct the analysis,
ask good questions. The LLM's job is everything else."*

This repo is the **engine** — the shared pipeline code and templates. Your knowledge
lives in **volumes**: separate (typically private) repos that are almost pure content
and run this engine as a dependency. One system, any number of brains.

```
sources (chat exports, inbox captures, manual drops)
  ↓ normalize            raw/ -> corpus/          one item per share, provenance frontmatter
  ↓ enrich               corpus urls -> enrichment/   transcripts, article text, READMEs, papers
  ↓ digest (LLM)         -> state/digests/         permanent per-item fact index
  ↓ taxonomy (LLM)       -> state/taxonomy.json    canonical topics + entities
  ↓ assemble (LLM)       -> wiki/                  topic + entity pages, [[wikilinked]], cited
  ↺ verify + lint        claims checked against sources; coverage enforced
```

## The invariants that make it trustworthy

- **Provenance at ingest** — who shared it, where, when. Captured once; irreproducible later.
- **Corpus is append-only; the wiki is a build artifact** — regenerable at any time,
  never the only home of a fact.
- **Every wiki claim cites corpus item ids** — never other wiki pages (kills
  self-citation drift). Citations are mechanically checkable (`kb-lint`).
- **Coverage is checkable** — every corpus item is cited by a page or explicitly
  ledgered as low-signal. Nothing silently vanishes.
- **Recency is first-class** — pages state "current state (as of ...)", keep superseded
  practice *as marked history*, and surface dated conflicts instead of resolving them.

## Using it

### A. Volume repo + engine as dependency (recommended)

```bash
mkdir my-dex && cd my-dex && git init
mkdir -p corpus enrichment wiki state raw bin .github/workflows
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/CLAUDE.md -o CLAUDE.md   # fill in Scope
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/kb -o bin/kb && chmod +x bin/kb
curl -sL https://raw.githubusercontent.com/leeovery/dex-engine/main/templates/inbox-caller.yml -o .github/workflows/inbox.yml
```

Then run pipeline commands from the volume root:

```bash
bin/kb normalize          # raw exports -> corpus items
bin/kb enrich run         # fetch everything behind every link (captions, articles, READMEs, papers, tweets)
bin/kb enrich whisper     # transcribe caption-less audio/video (needs OPENAI_API_KEY in .env)
bin/kb lint               # broken links, bad citations, orphans, staleness
bin/kb exclude out.json   # purge out-of-scope items, permanently
```

(`bin/kb` is a two-line shim around `uvx --from git+https://github.com/leeovery/dex-engine kb-<cmd>`.
Needs [uv](https://docs.astral.sh/uv/). All commands resolve paths from the current directory.)

The LLM half — digesting, taxonomy, page assembly, verification, answering questions —
is driven by the volume's `CLAUDE.md` (see `templates/CLAUDE.md`): open the volume in
Claude Code (or point a Cowork/scheduled agent at the repo) and ask it to ingest,
query, or lint. If you use Claude Code, the included **`dex-new-volume` skill**
(`.claude/skills/`) scaffolds a new volume interactively.

### B. Casual / local

Clone this repo, `mkdir volumes/anything`, work inside it. The engine only cares
about the directory it runs from. Good for trying the system before committing to
the volume-repo shape.

### C. Vendored

Copy `src/dex_engine/*.py` into your own repo if you want zero external dependencies.
You lose free engine updates; everything else works identically.

## Capture (the inbox)

Any HTTP client can file knowledge into a volume by **opening a GitHub issue**:
`title` = the URL, `body` = an optional "why". The included reusable workflow
(`.github/workflows/inbox-reusable.yml`, called by `templates/inbox-caller.yml`)
appends it to `state/inbox.md` and closes the issue. The next ingest triages the
inbox with full provenance and scope filtering.

That makes capture clients trivial: an iOS Shortcut (Share Sheet → one POST to
`/repos/<owner>/<volume>/issues` with a fine-grained PAT), a bookmarklet, a CLI
alias. Suggestion is cheap and untrusted by design — filtering happens at ingest,
inside the volume.

## Layout of a volume

```
CLAUDE.md        scope + operations (the agent's contract — this makes it a wiki
                 maintainer instead of a chatbot)
raw/             verbatim source exports (append-only, never edited)
corpus/          one item per share: YYYY/YYYY-MM-DD-<slug>-<id>.md (docs/schema.md)
enrichment/      <item-id>/<kind>.md — fetched content
wiki/            topics/ entities/ syntheses/ + index.md, log.md, pins.md
state/           digests/, taxonomy.json, enrichment ledger, exclusions, inbox.md,
                 normalize-config.json (volume-specific name maps / internal domains)
```

See `example/` for a tiny illustrative volume, and `docs/schema.md` for the corpus
item format.

## Operations (run by the agent, per the volume's CLAUDE.md)

- **ingest** — new items: enrich → digest → place in taxonomy → update affected wiki
  pages, index, log.
- **query** — answer from the wiki (index first, follow links, cite item ids); file
  answers that required real synthesis back under `wiki/syntheses/`.
- **lint** — `kb-lint` mechanical checks + agent judgment (contradictions, staleness,
  superseded claims). Errors in this system are findable, not silent.
