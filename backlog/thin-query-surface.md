# Thin query surface

A read surface for clients that cannot read files — the Claude app, ChatGPT,
a phone. Every query route today ends in a Claude Code session with the
instance folder on disk, which is why an owner without a terminal cannot ask
their own knowledge base anything.

This was considered and dismissed once, correctly, on the facts as they then
stood. Asked in the aikb days whether other projects should reach the
knowledge base over MCP, the owner ruled: "It'll never be an application that
wants access. It'll always be another Claude session, but in another project
as a sibling of this one." For that consumer MCP is strictly worse than
files — the session already has Read, Grep and link-following, and the wiki
was shaped for exactly that. The ruling is not wrong; its premise is gone.
The consumer now wanted is one that has no filesystem at all.

## Thin, not thick

Two designs, and the difference decides whether this costs tokens.

**Thin.** The server exposes primitives and the *calling* model does the
agentic search. Nothing is shelled out to a second session; the caller's own
inference is the search agent, which is all dex-query has ever been — a
procedure wrapped around grep and read.

- `search(query)` — grep across digests, wiki and corpus; return
  `{id, title, date, url, snippet}`
- `fetch(id)` — the digest, or the item with its note and provenance, or the
  full fetched source on request
- `page(name)` — a wiki page, its wikilinks resolvable by further calls
- resources — `wiki/index.md` and `state/taxonomy.json` attached up front, so
  the model starts holding the map
- a prompt — the dex-query procedure itself, shipped by the server, so the
  skill travels to clients that have no skills

**Thick.** One `ask(question)` tool, the server running its own agent session
against the instance and returning a cited answer. This is the variant that
spends tokens, stores a key, and takes tens of seconds. It buys consistent
quality on impatient clients and nothing else. Fallback at most.

The honest gap in thin: app clients are less patient than Claude Code — fewer
tool calls, smaller working context, more willing to answer from the first
hit. That is fixed by making each call high-yield (see the catalogue idea),
not by adding a retrieval engine.

## Writing back

`capture(url, note, dex)` belongs on the same server: "save this to my dex"
mid-conversation, writing the same inbox file through the same contents API.
No new principle is needed — a capture is already an untrusted suggestion,
scope-filtered at processing time inside the instance.

## Where it runs

Local stdio first: a day's work, no infra, no auth, and it serves the desktop
app reading local clones. It does nothing for a phone and nothing for anyone
without the repos on disk.

Remote HTTP is the one that meets the actual want. GitHub OAuth for identity
means the owner's GitHub account *is* their dex identity — no new account
system, and existing repo permissions decide what is readable. The corpus can
be read through the contents API on demand rather than mirrored.

Verify on a real client before designing around either: the current ChatGPT
connector contract (it has been shaped around `search`/`fetch`, which is why
those two should be implemented to spec and richer tools treated as
Claude-only), and whether Claude iOS custom connectors reach far enough. What
that check turns up belongs in `field-notes.md`.

Depends on nothing; improved considerably by `catalogue.md`. Multi-instance
questions belong to `instance-registry.md`.
