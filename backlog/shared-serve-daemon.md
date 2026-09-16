# One shared serve daemon

`dex serve` is registered globally, so every chat session on the machine
spawns its own copy. Measured on the owner's mac (2026-09-16, 77 `claude`
processes): 26 server pairs alive at once — a `uv tool uvx` supervisor at
~14 MB plus a Python child at ~16 MB each, ~780 MB resident in total, every
one of them having used under 1.2 s of CPU in thirteen days. The instances
are the same four in every case, and the reads are stateless, so the 26
processes are 26 copies of one answer. The idea is to serve them from one
process over loopback HTTP instead, and let the clients connect to it.

**The global registration is not the problem and is not up for
negotiation.** dex being available in every session, in any project, is the
point of `dex connect`. What is in question is only how many processes that
costs.

**What the fix already landed takes off the table.** The pin/commit work
(#149) cut a launch from ~2.7 s to ~0.24 s, so start-up latency is no
longer an argument for a daemon. The remaining case is memory and process
count alone: 26 pairs to 1, ~780 MB to ~16 MB. Judge it on that.

## What is already true

- `dex-serve` holds no state between calls — the roster is the arguments
  and every read hits the working tree — so several sessions sharing one
  process needs no session layer at all. `stateless_http=True` matches
  where the protocol itself went in the 2026-07-28 revision.
- The SDK ships the transport: `MCPServer.run(transport="streamable-http")`
  on `mcp` 2.1.1, and `uvicorn` is a hard dependency of `mcp`, so every
  instance environment can already serve it. No new dependency.
- Claude Code takes an HTTP server at user scope (`claude mcp add
  --transport http`), which is the shape `connect_code` already writes
  through. On v2.1.221+ `MCP_DISCOVERY_CACHE=1` makes it connect on first
  tool use rather than at session start, so a session that never asks dex
  anything pays nothing. An unreachable HTTP server is retried three times
  and then marked failed; it does not break the session.
- launchd can hold the port and start the daemon on the first connection:
  `launch_activate_socket` is reachable from Python through `ctypes`
  (verified), and `uvicorn.Server.serve(sockets=[...])` takes the socket it
  hands back. With an idle exit the daemon then exists only while someone
  is asking.

## What has to be decided

- **Socket-activated with idle exit, or always-on.** Activation is the
  honest answer to "zero cost when idle" but it is more moving parts, and
  a plist is a new artefact for `dex connect` to own.
- **How the daemon learns its pin moved.** It runs whatever the anchor
  pinned at launch, and a sync that bumps the pin does not reach it.
  Cheapest shape: check the anchor's pin per request and exit on a
  mismatch, letting activation restart it at the new commit. Always-on
  needs something else.
- **The desktop app stays stdio.** Its config takes a command, not a URL,
  and custom connectors refuse localhost, so it keeps its own child
  process. An `mcp-remote` bridge would work and buys nothing.
- **Loopback is not by itself safe.** The SDK's DNS-rebinding protection is
  OFF unless a `TransportSecuritySettings` is passed; bind 127.0.0.1 and
  set `allowed_hosts`/`allowed_origins` explicitly. The corpus records this
  as the standard localhost-transport hole
  (`2025-12-09-github-tobi-qmd-mini-cli-search-engine-f-d2f28b`).

## Considered and set aside

- **Exec a sync-built venv directly, dropping the `uv tool uvx` parent.**
  Saves ~14 MB per session and ~0.2 s, at the cost of a second
  distribution channel beside uvx — building, garbage-collecting and
  repairing venvs by hand. Moot for Claude Code if the daemon lands.
- **Drop MCP for Claude Code entirely, in favour of a CLI plus a skill.**
  Zero resident processes and a few thousand tokens of tool schema saved
  per session, and the corpus has real evidence for CLI-over-MCP
  (`2026-02-17-playwright-cli-vs-mcp-server-which-is-ac-0ee312`). Rejected
  for dex specifically: seven small tools is not where schema bloat bites,
  and paying process start-up on every call is worse than one warm
  process. The counter-evidence is in the corpus too
  (`2026-08-20-brijr-iris-website-screenshots-029964`).
