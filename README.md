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

You need [Claude Code](https://claude.com/claude-code) (or any Claude surface that can
work with a git repo) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/leeovery/dex && cd dex && claude
```

Then say:

> **/dex-new-instance**

Claude interviews you — what to call it, what it covers, where it should live — and
sets up everything: the repo, the structure, the scope rules, and (if you want) a
one-tap "save to my dex" shortcut for your phone (recipe: `templates/shortcut.md` —
one shortcut serves one instance silently or several with a picker). From then on you talk to your
knowledge base, never to the machinery.

## Living with it

- **Save from anywhere** — share a link from your phone or paste it into a session:
  "add this to dex". It gets fetched, transcribed if it's a video, filed with
  provenance, and woven into the relevant pages.
- **Ask anything** — "what's the current thinking on X?" Claude answers from your
  wiki with citations, prefers the newest material, and files genuinely new answers
  back in so they compound.
- **Trust but verify** — now and then, ask Claude to give it a health check:
  broken links, missing citations, stale pages, and contradictions get found and
  fixed. Corrections you make by hand are pinned and survive every rebuild.

One **dex**, many brains: each knowledge base is its own private *instance* repo
(dex-engineering, dex-cooking, one for your partner's business...) and this public
repo is the shared engine they all run on. Fix the engine once, every instance
benefits.

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

See `example/` for a three-file toy instance showing the shapes, and `docs/schema.md`
for the corpus format.

<details>
<summary><b>Under the hood</b> (for agents and the curious — humans never need this)</summary>

Instances run the engine's mechanical commands via a `bin/kb` shim
(`uvx --from git+https://github.com/leeovery/dex kb-<cmd>`, cwd = instance root):

| command | does |
|---|---|
| `kb-normalize` | raw chat exports → corpus items (Google Chat Takeout + DiscordChatExporter formats) |
| `kb-enrich run` | fetch behind every corpus URL: YouTube captions, article text (with Wayback fallback), GitHub repos/profiles, arXiv papers, tweets |
| `kb-enrich whisper` | transcribe caption-less media (OpenAI key in instance `.env`) |
| `kb-lint` | broken wikilinks, bad citations, orphan items, index drift, stale pages |
| `kb-exclude <json>` | permanently purge out-of-scope items (survives re-normalization) |

Capture inbox: clients write one file per capture into `state/inbox/` via the
GitHub contents API — a `.md` (URL + note) for links and text, or an image file
(plus optional `.md` sidecar note). No server-side machinery at all: the PUT is
the commit. Suggestion is untrusted by design — scope filtering happens at
ingest, inside the instance.

Instance layout: `CLAUDE.md` (scope + operations contract) · `raw/` (verbatim
exports) · `corpus/` (append-only items) · `enrichment/` · `wiki/`
(topics/entities/syntheses plus index, log, pins) · `state/` (digests, taxonomy,
ledgers, inbox, normalize-config.json).

</details>
