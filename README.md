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

## The lens

Every dex reads through a lens: a short note, in your own words, of what it
reads things for. Whatever you share into it is read from that angle, so the
lens decides what gets pulled out, how it is filed and what the wiki says
about it.

Share a gardening company's website into three dexes and you get three
different readings: your design dex reads it for its layout, type and
photography, your business dex for how the company makes its money, and your
gardening dex for the plants. Nothing is turned away for its subject, because
sharing it was the decision and the lens only decides what to take from it.
Something is dropped only when the lens finds nothing in it at all, like a
stray chat message.

The lens lives in `lens.md`, written however suits you (sentences, a list or
an example), and you change it by asking Claude. A dex with no lens is a
general knowledge dex, which reads everything for its plain substance.

## Getting started

Setup is a single paste, after which Claude asks whether this is a new dex or
an existing one, asks what it should read for, installs it, arms the schedule
and sets up phone capture.

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
synthesis is filed back in so that it compounds instead of evaporating. Or
connect your dexes to the chat clients on your machine — the desktop app's
ordinary chat, and Claude Code at user level so every session in every project
can reach them — where you can ask them, and say "save this to my dex",
without opening the folder at all. That is one more paste, in a Claude Code
session:

```
Fetch and follow the instructions at https://raw.githubusercontent.com/leeovery/dex/main/docs/connect.md.
```

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
| **Instagram** | a public post's caption, author and date, read credential-free from the link-preview metadata instagram.com serves, with the media pulled through an embed proxy: video is transcribed, images are kept beside the caption. A private post parks and says to screenshot it |
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
  resolve to a real corpus item, uncited orphans, a map or index out of step
  with its inputs, pages older than
  their own sources, restated facts and ledger schema violations. Judgment
  repairs whatever the check finds.

## On the way

- **Watchers**: folders, Dropboxes, RSS and social feeds as standing capture
  sources feeding the same inbox, which is the next thing to be designed.
- **Hosted transcription**: any OpenAI-compatible provider already works, but a
  benchmarked recommended config is still to come.

Tracked in [`backlog/index.md`](backlog/index.md), one file per idea.

---

See [`example/`](example/) for a toy instance showing the shapes, and
[`instance/skills/dex-run/references/schema.md`](instance/skills/dex-run/references/schema.md)
for the corpus format.

<details>
<summary><b>Under the hood</b> (for agents and the curious; humans never need this)</summary>

Instances run the engine's mechanical commands via a `bin/dex` shim
(`uvx --from git+https://github.com/leeovery/dex@<pinned-commit> dex-<cmd>`,
cwd = instance root). The pin is one line in `.dex-engine-pin`, `<tag>
<commit>`, bumped by sync and read beside the shim rather than from the
working directory (chat clients spawn `serve` from anywhere). The shim
launches by the commit, which uv treats as immutable — no release lookup per
launch — and falls back to the tag for a pin an older sync wrote:

| command | does |
|---|---|
| `dex-enrich run` | drain the ledger work queue, fetching behind every corpus URL and file (captions, articles with Wayback fallback, GitHub, papers, X thread walk-up, podcast audio to transcript, Instagram posts with their media, document extraction) and report the session's cognitive work list |
| `dex-enrich status` | ledger summary, waiting cohorts, interrupted-session backstop, capability report (`--item <id>` for one item's ledger view) |
| `dex-enrich transcribe` | drain the waiting transcription cohort (whisper local floor or OpenAI-compatible API; `--limit`, `--model`) |
| `dex-enrich fetch` | fetch extra URLs into an existing item, ledgered as children (the harvest verb) |
| `dex-enrich compact` | rewrite the ledger to the latest line per unit, settling union merges |
| `dex-enrich mark` | heal one ledger entry: the sanctioned correction verb |
| `dex-enrich pass` | record a stage completion (harvest/digest/wiki) in `state/passes.jsonl` |
| `dex-enrich item new` | create a corpus item from a capture file (id rules and provenance; code writes frontmatter) |
| `dex-enrich item digest` | write an item's digest from a JSON payload — signal, topics and facts are the judgment, the file's shape is the engine's; the digest pass is recorded in the same call |
| `dex-enrich item describe` | write an item's description of one media file it carries from a text file — the reading is the judgment, the `media-<n>.md` slot and the first line naming the file are the engine's; the item's frontmatter is refreshed in the same call |
| `dex-enrich place` | apply placement judgment to `state/taxonomy.json` and `state/entity-members.json` from a JSON payload — define topics and entities, place/unplace items, drop fold-aways; validated whole and refused whole, both files rewritten deterministically, the map and index recompiled |
| `dex-normalize` | raw chat exports to corpus items (DiscordChatExporter JSON) |
| `dex-lint` | mechanical health check: a lens note, never a failure (`lens.md` still holding the seed's placeholders or unreadable, so the instance reads as general knowledge; no `lens.md` at all is a general knowledge dex and no note), wikilinks, citations (shortid flags included), orphans, map and index freshness (a byte diff against an in-memory recompile), stale pages, count drift, restated-fact warnings, ledger schema, ledger↔corpus integrity, cap fires, thread-completeness markers, digest shape, media drift and placement advisories, pass records (`--write` reconciles derived wiki frontmatter) |
| `dex-map` | compile the instance map into `state/map.json` — every topic and entity with counts, members and has-page, plus the typed relation graph (directed wikilink edges, weighted shared-member edges) — and render `wiki/index.md` from the same compile; both deterministic to the byte, from taxonomy, entity-members, corpus and wiki |
| `dex-exclude <json>` | permanently drop items that yield nothing through the instance's lens, with their media, enrichment, digest, pass records and ledger entries, surviving re-normalization |
| `dex-issue` | file one session-observed engine defect upstream from a JSON payload — mechanics only, refused whole on any content leak; the local record lands in `state/issue-reports.jsonl` |
| `dex-inbox` | materialize staged binary captures: release asset to `media/<id>/` (LFS), asset deleted (`ensure` creates the standing inbox release) |
| `dex-sync` | pin check and engine upgrade, migrations, machinery refresh, sync report (pending directives included, and a lens note when `lens.md` still holds the seed's placeholders or cannot be read): step 0 of every run |
| `dex-directive` | the engine's directives, performed by the run after its pull: `list` the pending ones in order, `show <n>` one's instructions and any materials rendered for this instance (`--materials` prints those alone, byte for byte), `done <n>` to run its check and record it in `state/directives.jsonl` only when every condition holds |
| `dex-render` | render a named report surface from a JSON payload as markdown, verbatim (state-bearing reports are never hand-drawn) |
| `dex-new <name>` | scaffold a new instance from the engine's bundled template |
| `dex-serve --instance <path>` | serve one or more instances to MCP clients over stdio: mechanical text search, item and wiki reads, the compiled-map reads (topics, entities, relation graph), instance-tagged and namespaced, plus capture into a named instance's inbox (repeat the flag per instance) |
| `dex-connect --instance <path>` | file the `dex` server with this machine's chat clients — the Claude desktop app and Claude Code — so their sessions reach these instances (`--client`, `--anchor`, `--config`; repeat `--instance` per instance, and re-run to change the list) |

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

Directives are the engine's other way into an instance. A migration is
mechanical by rule and never transforms content, so work on an instance's own
files that needs judgment ships as a directive instead, numbered in a sequence
of its own beside the migrations, with an intent, markdown instructions, a
check written in code, and where it needs them, materials the engine renders
for the instance, such as a template filled in with its name. Sync lists the
ones an instance has not completed, and
the run performs them after its pull and before the inbox, unattended and in
order, one commit each. Until they are done, the content commands
(`dex-inbox`, `dex-normalize`, `dex-enrich`, `dex-exclude`) refuse and point
at `dex-directive list`, and each directive command's output names the step
after it, so a session holding instructions older than the engine it just
synced still performs the directives before it touches any content.
`dex-directive done` records a directive in
`state/directives.jsonl` only when its check passes, so a session that ran out
of time or misread the instructions cannot mark the work done, and a directive
that fails its check is filed as an engine defect, as the refusal directs, and
tried again on the next run, with content work waiting until it completes.
A new instance records every shipped directive as done when it is created.
The first two bring an instance made by an older engine into the shape a new
one is born in: directive 1 reads the owner's own CLAUDE.md back from git
history and moves what it said the instance reads for into `lens.md`, and its
Discord facts into config (when it said nothing, no `lens.md` is written and
the instance reads as a general knowledge dex), and directive 2 rewrites the
README from the template, whose lens line links `lens.md` or, with none,
names a general knowledge dex.

Capture inbox: every capture is one `.md` in `inbox/`, written via the GitHub
contents API (the phone shortcut) or committed directly by the dex-capture
skill. The body is the URL and/or note. A capture that carried a binary (image,
PDF, any file) stages it as an asset on the repo's standing `inbox` release and
references it in frontmatter. Your knowledge base runs no server-side machinery
at all — no Actions, no webhooks, nothing writing to it but you and your own
sessions: the PUT is the commit, and the next run moves staged binaries into
`media/` where LFS applies. Sharing is the curation, so nothing is judged at
the door: every capture becomes an item, and the instance reads it through its
lens once its content has landed, dropping it only when that yields nothing.
An instance with no lens is a general knowledge dex, reading every share for
its general substance and dropping only content with nothing in it at all.
Full protocol: `docs/capture.md`.

Query surface: `dex-serve` is an MCP server — one process serving several
instances, stateless between calls, every call a fresh read of disk. Seven
tools — `search`, `fetch`, `page`, the three map reads (`topics`, `entities`,
`graph` — the graph trimmed to a readable default, opened up by `around`,
`min_weight` and `full`), and `capture` — hits tagged with the instance they
came from and ids namespaced `<instance>/<item-id>`, plus each instance's
`wiki/index.md`, `state/taxonomy.json` and compiled `state/map.json` attached
as resources. Hands, not an
agent: no model runs on that side and nothing is ranked, so the calling chat
does the searching with its own inference. What keeps an impatient caller
probing is prose rather than machinery (`serve/steering.py`) — connect-time
instructions carrying the doctrine, every instance's lens verbatim (a
general knowledge dex, one with no lens, is named as one to search for
anything) and the rule that a capture goes where the owner says, one
next-move line on each result, and a `dex-query` prompt read from the
wheel-bundled skill template (`template.py`) so that procedure keeps a
single home. `dex-connect` writes
the client-side half, for every chat client on the machine: one
`mcpServers.dex` entry per client, the same
launcher in each — an anchor instance's `bin/dex`, so the anchor's pin stays
the only version authority. The desktop app's copy is a merge into the JSON
file it owns, carrying an `env.PATH` captured from the installing shell
because a GUI-spawned child inherits launchd's bare PATH; Claude Code's is
written by shelling out to `claude mcp` (remove, then add — an add onto an
existing name refuses rather than replacing) and carries no PATH, since a session
already has a real one. A client that is not installed is skipped, never
guessed at, and a run that reached none of them fails rather than reporting
success. Sync reads the same configs — read-only, silent wherever a client
cannot be — and puts each gap on its report separately, so a session offers
the connection rather than the owner having to know it exists. Setup:
`docs/connect.md`.

Instance layout: `CLAUDE.md` (the same in every instance, synced and
engine-owned, left alone while git history does not hold it; imports the
contract and the lens) · `lens.md` (what the instance reads for; the
owner's, never synced; absent for a general knowledge dex) · `README.md` (the owner's; how the instance shows on
GitHub) ·
`.claude/` (synced skills + `dex-contract.md`) · `bin/dex` (the shim) ·
`inbox/` (pending captures) · `raw/` (verbatim exports) · `corpus/`
(append-only items) · `enrichment/` · `media/` (captured binaries, LFS) ·
`wiki/` (topics/entities/syntheses plus index, log, pins) · `state/` (digests,
taxonomy, the compiled map, the ledger and other append-only JSONL,
config.json) · `cache/` (gitignored ephemera) · `.dex-engine-pin` (the engine
release this instance runs, tag and commit).

</details>
