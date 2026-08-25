<div align="center">

# 🧠 dex

**Your own knowledge base, maintained for you by Claude.**

Save things with one tap. Ask questions later. Everything cited, dated, and kept current.

[![Runs on Claude](https://img.shields.io/badge/runs%20on-Claude-cc785c)](https://claude.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)](https://github.com/leeovery/dex/blob/main/pyproject.toml)
[![Powered by uv](https://img.shields.io/badge/powered%20by-uv-261230)](https://docs.astral.sh/uv/)

</div>

---

**dex** is a personal or business knowledge base that Claude helps build and
maintain. It takes in the links worth keeping, wherever they get shared from,
reads each one properly, and files what it learns into a wiki that answers
questions with citations.

What that means in practice:

- **Nothing stays a bookmark.** Videos are transcribed, PDFs and office
  documents are read, X threads are walked back to their first post, and links
  worth following are followed.
- **Every answer has receipts.** Each claim on a page cites the corpus item it
  came from, and every citation is checked mechanically on every run.
- **Pages stay current.** New material rewrites the pages it affects, and the
  claims it supersedes are kept and marked instead of deleted.
- **It compounds.** Everything in it is there because you judged it worth
  keeping, so the answers carry your judgement instead of the internet's average
  opinion.
- **Nothing needs running.** Claude operates the whole system on a schedule, and
  the only work left to you is deciding what to save and correcting what comes
  back wrong.

That split follows Karpathy's
[llm-wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
pattern: *"The human's job is to curate sources, direct the analysis, ask good
questions. The LLM's job is everything else."*

## What one shared link turns into

```
                     an X thread, shared from a phone
                                    │
              ┌──────────────┬──────┴───────┬──────────────┐
              ▼              ▼              ▼              ▼
         the thread      the repo       the paper      the talk
         walked back     it links       it cites       it links
         to its root                                   captions, or
         every post,     README,        fetched        audio pulled
         attributed      metadata       from arXiv     and transcribed
              │              │              │              │
              └──────────────┴──────┬───────┴──────────────┘
                                    ▼
                      one corpus item, stamped with
                        who shared it, where, when
                                    │
                                    ▼
                    digest ─▶ topics ─▶ the wiki pages
                    that this changes, rewritten with
                    dated claims and citations
                                    │
                                    ▼
          "what is the current thinking on chunking strategy?"
           answered from those pages, newest first, citing the
           thread and everything else filed against the problem
```

## Getting started

Setup is a single paste, after which Claude asks whether this is a new dex or
an existing one, interviews you about scope, installs it, arms the schedule and
sets up phone capture.

The right home is **Claude Code in the [Claude desktop app](https://claude.com/download)**,
which is the only surface that can arm the schedule: a local routine that runs
dex on your own machine, with your GitHub auth and your network.

1. Open the app, go to **Code**, start a new chat.
2. Set the model to **Opus-class**. The work is judgment, and lighter models
   degrade it invisibly.
3. Point the chat at your home or code folder. Setup asks where the dex should
   actually live and puts it there.
4. Paste this and press enter:

```
Fetch and follow the instructions at https://raw.githubusercontent.com/leeovery/dex/main/docs/start.md.
```

### Other ways in

- **A terminal.** The same paste installs everything identically there, but it
  cannot arm the routine, so the trigger has to come from somewhere else: cron,
  launchd, or nothing at all. A schedule is not required for the system to
  work, since captures wait in the inbox until something runs and none of them
  expire.
- **Another agent.** dex is not Claude-specific, and anything that reads skills
  should be able to drive it, though nothing else has been tested.
- **By hand.** [docs/start.md](docs/start.md) is the procedure itself, if you
  would rather follow it yourself.

## Day to day

**Capture.** There is no dex app to install, because capture is a protocol: one
markdown file lands in the instance's `inbox/`, and any HTTPS client can
implement it ([`docs/capture.md`](docs/capture.md)). The clients that exist
today are the phone shortcut, which shares from any app in one tap
([`docs/shortcut.md`](docs/shortcut.md)); any Claude session, where "add this to
dex" commits and pushes it; and chat exports dropped in for backfill.

Capture and processing are deliberately separate. A save is instant and always
succeeds; the reading happens on the next run. A capture carrying a photo or a
PDF stages the binary outside git, so history stays text only.

**Questions.** Open a Claude Code session on the folder and ask. Answers come
from the wiki with citations, newest material preferred, and any genuinely new
synthesis is filed back in so that it compounds instead of evaporating.

**Maintenance.** A health check runs on its own whenever the last one is more
than a week old, and the corrections you make by hand are pinned and reapplied
after every rebuild.

One dex, many brains: each knowledge base is its own private *instance* repo
(dex-cooking, dex-woodworking, one for your partner's business) and this public
repo is the shared engine they all run on.

## How it works

The system splits into two kinds of work and keeps them strictly apart: the
engine fetches, moves and verifies deterministically and without judgment,
while Claude does the reading, the deciding and the writing.

```
 ENGINE: mechanical, deterministic
 ┌──────────────────────────────────────────────────────────────────────┐
 │  capture ──▶ corpus item ──▶ work queue ──▶ enrichment               │
 │  one file    who · where     every URL      transcripts, article     │
 │  in inbox/   when · why      and file       text, extracted docs     │
 └──────────────────────────────────────────────────────┬───────────────┘
                                                        ▼
 CLAUDE: judgment
 ┌──────────────────────────────────────────────────────────────────────┐
 │  digest ──▶ topics + entities ──▶ wiki pages                         │
 │  a fact     discovered            dated claims,                      │
 │  index      bottom-up             citations, [[wikilinks]]           │
 └──────────────────────────────────────────────────────┬───────────────┘
                                                        ▼
        lint + verify: every citation resolves to a real corpus item,
        and every item is cited on a page or explicitly ledgered
```

The corpus is the truth and the wiki is a build artifact. Pages can be
regenerated from scratch without losing anything, which is what makes
rewriting them safe.

### The work queue

Everything fetchable becomes an entry in an append-only ledger, and a run
drains whatever is not yet finished. Work that fails does not disappear; it
parks somewhere with a reason attached and comes back round.

```
        ┌────────────────────────────────────────────────────┐
   ┌───▶│                    WORK QUEUE                      │
   │    │        append-only; a run drains what is left      │
   │    └─────────────────────────┬──────────────────────────┘
   │                              ▼
   │                      detect ──▶ fetch
   │                                  │
   │        enrichment file ◀─────────┤  got it
   │                                  │
   ├──────────────────────────────────┤  links worth following
   │                                  │
   ├──────────────────────────────────┤  the server lied about the type
   │                                  │
   ├── waiting ◀──────────────────────┤  needs a transcript or extraction
   │     returns when a provider appears
   │                                  │
   ├── blocked ◀──────────────────────┤  403 · 429 · 5xx
   │     retried every run; parked for judgment at 5
   │                                  │
   └── error ◀────────────────────────┤  an engine bug, files an issue
         retried once the engine is newer
                                      │
            dead ◀────────────────────┘  404 · no such domain (final)
```

**Blocked is never dead.** A Cloudflare challenge, a rate limit or a bad
gateway means the world is misbehaving, which is not the same as the thing
being gone. Only a real 404 or a domain that does not resolve is treated as
final. A fetch that returns 200 but comes back empty also parks for judgment.
An empty result means our tooling could not read the page, which is a very
different problem from the page having nothing on it.

## What it can read

| source | what it does |
|---|---|
| **YouTube** | captions where they exist, otherwise the audio is downloaded and transcribed |
| **X** | walks the thread up to its root, giving every post in reading order, each attributed, with quoted posts inline. Sharing a thread's *last* post rolls up the whole thing. An incomplete chain is recorded as incomplete and never presented as whole |
| **GitHub** | repos and profiles: README, description, metadata |
| **Papers** | arXiv and friends |
| **Podcasts** | an Apple or Spotify link resolves to the show's RSS and the real audio enclosure, then transcribes it. Show notes come from the feed, which is richer than the page |
| **Web** | article text with the boilerplate stripped, falling back to the Wayback Machine when the live page has gone |
| **Files** | PDF, Word, PowerPoint, Excel, OpenDocument, RTF, EPUB and CSV. Text is extracted and embedded images are pulled out alongside it. Scanned pages route to OCR |

Three things sit on top of that, and they are where the real work happens:

- **Links get followed with judgment.** At every fetched page, Claude decides
  which links are primary artifacts of the thing itself, such as the project's
  repo, the paper or the docs, and promotes those. Blogrolls, footers and
  "similar projects" are never promoted, at any depth. The engine never makes
  that call itself; it only enforces the bounds, which are 4 levels deep and 12
  fetched URLs per item.
- **Transcription has a free floor.** Local whisper is the default and needs no
  API key, no GPU and no account. Point it at any OpenAI-compatible endpoint if
  you want the speed instead. Long audio is chunked, so upload limits do not
  apply.
- **A source that lies gets corrected mid-fetch.** A URL that claims to be a web
  page and turns out to be a PDF is re-detected from its actual bytes and
  re-routed, in the same run, in either direction.

## It looks after itself

- **Failed work retries on its own.** Blocked fetches retry every run. Engine
  bugs retry once the engine is newer, since there is no point running a
  deterministic bug against the same code twice.
- **It reports its own bugs.** An engine exception files an issue at this repo,
  deduplicated by fingerprint. Issue bodies carry no free text at all: version,
  command, error class, an engine-frames-only traceback, and a hash of the URL.
  This is not a scrubber that might miss something. The field does not exist, so
  your content has nowhere to leak through.
- **Fixes arrive by themselves.** Instances pin an engine release. Sync checks
  for a newer one, bumps the pin, runs any state migrations and refreshes the
  synced machinery, all before anything touches state, at the start of every
  run. That closes the loop: a bug files an issue, the fix ships in a release,
  sync picks it up, and the work that failed runs again and succeeds.
- **The wiki is checked mechanically.** Broken wikilinks, citations that do not
  resolve to a real corpus item, uncited orphans, index drift, pages older than
  their own sources, restated facts and ledger schema violations. Judgment
  repairs whatever the check finds.

## On the way

- **Watchers**: folders, Dropboxes, RSS and social feeds as standing capture
  sources feeding the same inbox, which is the next thing to be designed.
- **Instagram**: no clean API exists, and the approach is not settled.
- **Hosted transcription**: any OpenAI-compatible provider already works, but a
  benchmarked recommended config is still to come.

Tracked in [`design/roadmap.md`](design/roadmap.md).

---

See [`example/`](example/) for a toy instance showing the shapes, and
[`instance/skills/dex-run/references/schema.md`](instance/skills/dex-run/references/schema.md)
for the corpus format.

<details>
<summary><b>Under the hood</b> (for agents and the curious; humans never need this)</summary>

Instances run the engine's mechanical commands via a `bin/dex` shim
(`uvx --from git+https://github.com/leeovery/dex@<pinned-tag> dex-<cmd>`,
cwd = instance root; the tag lives in `.dex-engine-pin`, bumped by sync):

| command | does |
|---|---|
| `dex-enrich run` | drain the ledger work queue, fetching behind every corpus URL and file (captions, articles with Wayback fallback, GitHub, papers, X thread walk-up, podcast audio to transcript, document extraction) and report the session's cognitive work list |
| `dex-enrich status` | ledger summary, waiting cohorts, interrupted-session backstop, capability report (`--item <id>` for one item's ledger view) |
| `dex-enrich transcribe` | drain the waiting transcription cohort (whisper local floor or OpenAI-compatible API; `--limit`, `--model`) |
| `dex-enrich fetch` | fetch extra URLs into an existing item, ledgered as children (the harvest verb) |
| `dex-enrich compact` | rewrite the ledger to the latest line per unit, settling union merges |
| `dex-enrich mark` | heal one ledger entry: the sanctioned correction verb |
| `dex-enrich pass` | record a stage completion (harvest/digest/wiki) in `state/passes.jsonl` |
| `dex-enrich item new` | create a corpus item from a capture file (id rules and provenance; code writes frontmatter) |
| `dex-normalize` | raw chat exports to corpus items (DiscordChatExporter JSON) |
| `dex-lint` | mechanical health check: wikilinks, citations (shortid flags included), orphans, index drift, stale pages, count drift, restated-fact warnings, ledger schema, ledger↔corpus integrity, cap fires, thread-completeness markers, digest shape, pass records (`--write` reconciles derived wiki frontmatter) |
| `dex-exclude <json>` | permanently purge out-of-scope items — corpus file, enrichment, ledger entries — surviving re-normalization |
| `dex-inbox` | materialize staged binary captures: release asset to `media/<id>/` (LFS), asset deleted (`ensure` creates the standing inbox release) |
| `dex-sync` | pin check and engine upgrade, migrations, machinery refresh, sync report: step 0 of every run |
| `dex-render` | render a named report surface from a JSON payload as markdown, verbatim (skills never hand-draw reports) |
| `dex-new <name>` | scaffold a new instance from the engine's bundled template |

The pipeline is ledger-driven (`state/enrichment-ledger.jsonl` is the work
queue, append-only, last-line-per-unit): drivers per source shape, one central
failure classifier (blocked is never dead), capabilities with a free local
floor (faster-whisper transcription, document extraction), and an issue filer
that reports engine bugs upstream, sanitized by construction, so that every
instance heals through releases and sync.

Every run completes end-to-end inside one Claude session. The run report is the
session's work list, and the same session finishes the cognitive steps for
everything on it. There is no headless daemon and no handoff. Entries that
survive a session are `waiting`, `blocked`, `error` or `manual`, each parked for
a stated reason and each printed on the report.

Capture inbox: every capture is one `.md` in `inbox/`, written via the GitHub
contents API (the phone shortcut) or committed directly by the dex-capture
skill. The body is the URL and/or note. A capture that carried a binary (image,
PDF, any file) stages it as an asset on the repo's standing `inbox` release and
references it in frontmatter. There is no server-side machinery at all: the PUT
is the commit, and the next run moves staged binaries into `media/` where LFS
applies. Suggestion is untrusted by design, so scope filtering happens at
processing time, inside the instance. Full protocol: `docs/capture.md`.

Instance layout: `CLAUDE.md` (scope and operations contract) · `inbox/` (pending
captures) · `raw/` (verbatim exports) · `corpus/` (append-only items) ·
`enrichment/` · `media/` (captured binaries, LFS) · `wiki/`
(topics/entities/syntheses plus index, log, pins) · `state/` (digests, taxonomy,
the ledger and other append-only JSONL, config.json) · `cache/` (gitignored
ephemera) · `.dex-engine-pin` (the engine release this instance runs).

</details>
