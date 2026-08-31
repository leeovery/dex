# Thin query surface

> Designed 2026-08-30, from the backlog idea of the same name. Not yet
> implemented. Facts about the Claude desktop app's MCP handling are
> **from knowledge, unverified on a device** unless marked otherwise;
> step 0 of the implementation is the verification spike, and what it
> finds lands in `field-notes.md`.

A read-and-capture surface for chat clients that cannot read files. Every
query route before this ends in a Claude Code session with the instance
folder on disk; an owner in the Claude desktop app's plain chat cannot ask
their own knowledge base anything, and cannot save to it mid-conversation.

The shape is an MCP server, and the word that matters is **thin**: the
server is hands, not an agent. It exposes mechanical primitives — grep,
read, write-an-inbox-file — and the *calling* chat model does the agentic
search with its own inference, exactly as a Claude Code session does with
Read and Grep. No model runs server-side, no key is stored, no tokens are
spent, calls answer in milliseconds. Agentic search was never a smarter
primitive; it is dumb probes in a loop driven by judgment, and the wiki's
curation (taxonomy, index, digests, wikilinks) is what makes dumb probes
land somewhere structured. The judgment was spent at write time so read
time can be cheap.

Two releases, deliberately:

- **v1 — desktop, local stdio.** The Claude desktop app spawns the server
  as a child process on the owner's machine; it reads the instance working
  trees directly. The scheduled task already keeps the clones fresh.
  Serves desktop chat (the target), and Claude Code sessions in sibling
  projects as a bonus.
- **v2 — remote HTTP.** For the phone, cloud Cowork, ChatGPT, and owners
  with no computer: a hosted server, GitHub OAuth for identity (the
  owner's GitHub account *is* their dex identity, repo permissions decide
  what is readable), the corpus read through the contents API. Not
  designed here; designing v1's tool surface well is what makes v2 mostly
  transport.

## 1. One process, all instances

The process count is per server entry, not per instance. The desktop app
spawns one child process at launch; it takes the instance roots as
arguments and serves them all. Ten conversations against four instances
is still one process, stateless between calls — every call is a fresh
read of disk, so concurrent conversations do not interact and the
scheduled task's pulls are visible immediately.

The instance roster comes entirely from the caller's arguments. The
engine stays instance-unaware, per the standing separation; the config
args *are* the registry for v1. (`backlog/instance-registry.md` keeps the
larger question — a shared registry that the shortcut's instance
dictionary could merge into. This design does not decide it.)

Instance boundaries are preserved at the content level, not the plumbing
level: every hit is instance-tagged, item ids are namespaced
(`<instance>/<item-id>`, instance name = directory basename), and nothing
is merged across instances. An unqualified search fans out to all of
them; a scoped question routes by the roster (§3).

## 2. The tools

- `search(query, instance?)` — mechanical text search across digests,
  wiki, and corpus notes; returns `{id, title, date, url, snippet,
  instance}` rows. Plain Python scanning, case-insensitive: at a few
  hundred items per instance there is no index to build and no ripgrep
  dependency to require. Raw hits, never a ranked answer.
- `fetch(id)` — the item: digest, owner's verbatim note, provenance;
  the full fetched source on request (`full=true`).
- `page(name, instance)` — a wiki page, its wikilinks resolvable by
  further `page` calls.
- `capture(url, note, instance)` — writes the same inbox file every
  other client writes, and commits it in the instance repo. "Save this
  to my dex" mid-conversation. Local commit, no push — matching the
  in-session dex-capture behaviour: the server is a same-machine client,
  and the shortcut's contents-API PUT exists only because the phone has
  no clone. No new trust principle: a capture is already an untrusted
  suggestion, scope-filtered at processing time inside the instance —
  which also bounds the prompt-injection surface (a hostile page talking
  a chat model into capturing garbage produces a suggestion the ingest
  session judges, not a corpus write).

Resources, attached per instance so the model starts holding the map:
`wiki/index.md` and `state/taxonomy.json`.

**No retrieval engine.** No embeddings, no BM25, reaffirming the
catalogue file's ruling: the corpus fits in a context window and the
taxonomy is a curated routing table that beats keyword ranking. A
ranking layer would fix client impatience by taking the loop away from
the model, which is backwards. The catalogue itself
(`backlog/catalogue.md`) is deferred to v2, where a call-starved remote
client needs the whole map in one read; it arrives as one more resource
plus richer search results, changing no tool signatures — which is why
deferring it costs nothing structurally. Pull it forward only if v1
field use shows the desktop model answering badly from too few probes.

## 3. The prompt-engineering surface

A chat model is less patient than Claude Code — two or three calls where
Claude Code makes ten. The counter is steering, not machinery, and MCP
offers four slots:

1. **Server instructions** (handed to the client at connect time) carry
   the doctrine once: raw hits, not ranked answers; follow wikilinks;
   keep probing until nothing relevant remains unexplored; answer only
   from fetched material and cite item ids. They also carry the roster —
   each instance's CLAUDE.md included verbatim (it holds only identity
   and scope, by the scaffold contract), so the model routes "zone 2
   training → health" without a tool call — and the reach-for-dex nudge:
   if the question falls inside any instance's scope, search dex before
   answering from general knowledge.
2. **Tool descriptions**, always in context: `search` says "returns
   candidate probes, rarely the answer — expect to reformulate and call
   again."
3. **Result footers**, one or two sentences, dynamic where cheap: a
   `search` result ends with the next move ("fetch promising ids; search
   again with new terms"), a `page` result lists its unresolved
   wikilinks as `page()` calls. Footers carry the next move; the
   instructions carry the doctrine; neither repeats the other, because a
   footer on every result that restates doctrine taxes context until the
   model skims it.
4. **An MCP prompt** — the full dex-query procedure, user-invocable for
   a deep pull. Served **from the wheel-bundled skill template**, not a
   second copy: the dex-query skill and the server must not become two
   drifting statements of one procedure. The micro-prompts (footers,
   descriptions) are server-owned; the procedure has one home.

## 4. Version: one authority, no second pin

A hard-coded tag in the MCP config is a rot vector: sync bumps every
instance's `.dex-engine-pin` while the config stays frozen at the day it
was written, and nothing tells the owner.

So the config never names a version. Its command is an **anchor
instance's own `bin/dex`** — `<anchor>/bin/dex serve --instance … ` —
and the shim already resolves that instance's pin. Sync bumps the pin;
the next app launch runs the matching server; the config only changes if
the launch contract itself changes. One version authority, zero new
machinery.

Accepted wrinkle: the anchor's pin governs, not the newest across
instances, so the server can briefly read an instance whose pin (and
state) is ahead or behind — an invariant the shim never violates when
running inside one instance. Accepted because the read surface is
markdown and contract-documented JSON, the inbox capture format is
already frozen by deployed phone shortcuts, and sync is step 0 of every
scheduled run, so instances converge within a day.

Consequences: the shim gains a `serve` verb (four-shell tests apply —
the bashism rules for `instance/dex` hold), and a new engine version is
picked up at app relaunch, not mid-session.

## 5. Setup: one paste, per machine

Same pattern as `docs/start.md`: a `docs/connect.md` fetched raw by a
one-liner the owner pastes into any Claude Code session (every owner has
one — it is how they run dex at all). The desktop chat cannot install
itself; that is the very gap being closed.

The labour splits on the house line:

- **Cognitive** (the session, following connect.md): find the instances
  on disk, confirm the list with the owner, handle the odd machine,
  choose the anchor, say "relaunch the app and try a question".
- **Mechanical** (an engine verb, `dex connect --instance <path> …`):
  locate the desktop app's config file, **merge** the `dex` server entry
  into any existing `mcpServers`, write it back. Deterministic JSON
  merging is exactly what engine commands are for — a freehand edit that
  clobbers the owner's other servers is the failure mode.

One trap the spike must settle **(unverified, known GUI-app class of
problem)**: a macOS GUI app spawns children with the stripped launchd
PATH, so `uvx` inside the shim may not resolve when the desktop app is
the parent. `dex connect` likely writes an `env` block (or absolute
paths) into the server entry; the spike decides which, and the answer
goes in `field-notes.md`.

## 6. Clients, honestly

- **Desktop chat** — the target; the mechanism's home.
- **Claude Code sibling sessions** — the same server via `claude mcp
  add`; redundant inside an instance (the session has the real files),
  but it gives any other project `search`/`capture` against the owner's
  dexes with no instance folder in sight. The official mechanism
  `backlog/sibling-session-capture.md` asks for falls out of this design.
  *(Amended 2026-08-31 — §11. As written this said the server serves the
  client, and was read as setup wiring it. It did not: `connect` wrote one
  file. It does now.)*
- **Cowork local** — the session already has the folder mounted and can
  read it directly; whether host-configured stdio servers are bridged
  into the sandbox at all is unverified and doubtful (a host-side
  process piped into the VM is an egress hatch the sandbox may
  deliberately not offer). A ten-minute device check, only if wanted.
- **Cowork cloud, phone, ChatGPT** — a local stdio server categorically
  cannot reach them. v2's column. The ChatGPT connector contract has
  been shaped around `search`/`fetch`, which is one more reason those
  two are implemented to spec and the richer tools treated as
  Claude-first.

## 7. What this design rejects, and why

- **The thick server** — one `ask(question)` tool running a server-side
  agent and returning a cited answer. Spends tokens, stores a key, takes
  tens of seconds, and buys only consistent quality on impatient
  clients. Fallback at most, and not built now.
- **A retrieval layer** (embeddings, BM25) — §2. Revisit around ten
  thousand items, and then as ranking over grep results, never as a
  layer underneath everything.
- **A second version pin** in the MCP config — §4's rot vector.
- **One server process per instance** — cleaner invariants, worse
  everything else: N config entries, N processes, and no cross-instance
  fan-out.
- **The `.mcpb` desktop-extension bundle for v1** — a real path (zip +
  manifest, native install UI, a `directory` picker that could gather
  the roster), but it is a distribution artifact to repackage every
  iteration, and the one-liner already reaches everyone who can run an
  instance today. It earns its place only for an owner with Claude
  Desktop and no Claude Code — the app-only owner, v2's territory.
- **Waiting for the catalogue** — §2; additive later, needed never for
  v1 to ship.

## 8. Architecture

- `src/dex_engine/serve/` — the server module; `dex-serve` the entry
  point; a thin CLI over an injected roster of `Instance` objects, zero
  import-time state, like every other command.
- Reads go through the existing seams: `corpus.py` stays the one
  frontmatter read point; capture writes the one inbox format.
- The `mcp` SDK lands as a **core dependency** (re-locked in the same
  commit, per the standing rule). An extra (`dex-engine[serve]`) would
  keep the pipeline commands' resolves lean but adds quoting fragility
  through the POSIX shim; core is simpler and the cost is a slightly
  fatter resolve everywhere. Revisit only if resolve time annoys.
- Tests run the server in-process through the SDK's client — no
  subprocess theatre, no live checks: the server touches no third
  party, so `live.yml` gains nothing.
- Everything mechanical, nothing cognitive: the server never calls a
  model, never composes an answer, never judges relevance. The judgment
  is the caller's.

## 9. Implementation plan

0. **The verification spike.** On a real machine: the desktop config
   file's location and merge behaviour; whether server `instructions`,
   resources, and prompts all surface in desktop chat as expected; the
   GUI PATH trap (§5); spawn-at-launch and relaunch-to-update. Findings
   to `field-notes.md`. Cheap, and everything after it stands on it.
1. **The server core** — roster construction, `search`/`fetch`/`page`,
   resources, namespaced ids; in-process tests over a fixture instance
   shaped like `example/`.
2. **Capture** — the inbox write + commit, sharing the format with
   `capture.py`'s machinery; tests including the untrusted-suggestion
   path (a capture with a scope-foreign URL still lands; judgment is
   the ingest session's).
3. **The steering surface** — instructions, descriptions, footers, the
   dex-query MCP prompt served from the bundled template.
4. **The shim verb + connect** — `serve` in `instance/dex` (four-shell
   tests), `dex connect` with the JSON merge, `docs/connect.md`.
   Entry points, the shim usage line, and the README command table move
   together, per the standing rule.
5. **Docs + README** — connect.md's use-side orientation ("ask your dex
   from any chat; say 'save this to my dex'"), README's under-the-hood
   mirror.

Mutation audit on the modules the work touches before it is called
complete, per the standing rule for anything with pipeline-adjacent
logic — `serve/` and whatever `capture.py` grows.

Instance rollout after release: `bin/dex sync` in every maintained
instance (the shim changes), then each owner's one-time
`docs/connect.md` paste.

## 10. Open, deliberately

- Everything §0 verifies: desktop config mechanics, instructions/
  resources/prompts support in desktop chat, the GUI PATH answer.
- Whether Cowork-local bridges stdio MCP at all (§6) — checked only if
  someone wants it.
- How patient the desktop chat model actually is with thin probes — the
  field question that decides whether the catalogue pulls forward.
- Corrections over this channel. `backlog/owner-curation-surface.md`
  stays in the backlog; its likely shape (a correction is itself a
  capture) rides this server's `capture` tool unchanged when it is
  designed.
- The registry's final home and the shortcut-dictionary merge
  (`backlog/instance-registry.md`, `backlog/shortcut-per-instance-tokens.md`).
- v2 wholesale: transport, OAuth, the contents-API read path, the
  catalogue's committed-vs-cache decision, ChatGPT's connector contract
  verified on a real client.

## 11. Amendment, 2026-08-31: clients are set up, not merely supported

Shipped in #116 and #118, and wrong in one place. §5 split the setup labour
for exactly one client, the desktop app, while §6 listed Claude Code as a
client the server serves — true of the server, and read by everyone
(including its author, in conversation) as a claim that setup wired it.
Nothing wired it. The owner who was told "it works in Claude Code" opened
`/mcp`, saw no dex, and had been given no way to fix it: the two clients read
different files, and the Code tab inside the desktop app is Claude Code, so
sharing a process with the app buys nothing.

Nothing about §1's one-process roster or §4's anchor changes. The command is
still one instance's `bin/dex`, that instance's pin is still the only version
authority, and both clients get identical argv. What changes is the number of
files written and who is asked.

**Detection gates the question.** `command -v claude` for Claude Code, the
config file's existence for the desktop app. A client that is not installed is
never mentioned — not asked about, not written, not reported as a gap. Absence
keeps three answers rather than two, because they have three different fixes:
not installed, never launched, and — off macOS, where no config location is
verified — not locatable without `--config`.

**Claude Code is written through `claude mcp`, never by merging its config.**
`~/.claude.json` carries live session and project state and is rewritten by
every running session; a read-modify-write from here can drop a concurrent
write. This is the one place where the codebase's "one serialization boundary"
loses to "another program owns this file and is writing to it right now".
Reading it is still safe, and the gap check does exactly that. Scope is
`user` — the point is dex from *any* session, and per-project would mean
re-running forever, while costing an unrelated session nothing until a tool is
called, which is `backlog/sibling-session-capture.md`'s own hard constraint.

Three device-verified facts shape that write, and are in `field-notes.md`:
`add` REFUSES a name that already exists rather than replacing it, so every
write removes first — and `remove` of an absent name is *itself* an error, so
that remove's exit code is ignored or every first-time write fails; the entry
carries no `env.PATH`, because Claude Code inherits a real one and an
install-day snapshot would be a downgrade; and a user-scope server reaches new
sessions only.

The first two were recorded backwards on the first pass, from a probe that
piped the CLI into `tail` and read `tail`'s exit code. The stub in the tests
encoded the wrong behaviour and the suite passed; running the real command
against a real machine is what found it.

**A run that reached no client fails.** Two skip lines and a zero exit is how
an owner ends up believing they are connected.

**The gap check is per client.** It read one config and answered one bit for
two clients, so the state that produced this amendment — wired into the app,
not into Claude Code — reported as fine. It also now catches an entry whose
command has left the machine: listing a root is not the same as being able to
launch it, and the anchor's shim is checked at write time and never again.

**One paste, whatever changed.** Adding an instance, adding a client declined
earlier, or repairing a broken entry is the same `docs/connect.md` paste. That
is stated in the hand-off, not only in the doc, because an owner who declined
a client will not reread the doc.
