# dex — field notes

Hard-earned facts about the platforms dex builds on, verified by testing,
stated nowhere official. For the agent building on these surfaces, never
for instance owners. Check the relevant section before building on a
platform named here; append what a session verifies the hard way, grouped
under its platform, with a date where staleness would matter. Knowledge in
this file was paid for — do not delete a fact without re-verifying it is
no longer true.

## Apple Shortcuts

- Shared iCloud shortcut links are frozen snapshots: edits never propagate;
  each re-share mints a new URL; importers must reinstall and re-answer
  import questions. Fields with import questions are auto-cleared in the
  shared copy.
- Base64 Encode must have Line Breaks = None (GitHub rejects wrapped
  base64).
- Variable chips can go stale after nearby edits: the chip renders normally
  but resolves empty at run time. Symptom: "Cannot parse response" on a
  request whose config looks correct. Fix: delete and re-insert the chips.
- Get Images from Input dereferences URL inputs (fetches them); the X app's
  share URL redirects through a non-HTTPS scheme and errors. Links must be
  detected and diverted before any Get Images call.

## GitHub API and tokens

- GitHub returns 404-shaped JSON for repos a token can't reach — Shortcuts
  shows a checkmark; silent failure.
- Instance repos owned by orgs can't use no-expiration fine-grained PATs;
  classic PAT (repo scope) is the workaround. Fine-grained tokens span one
  account or organization at a time.

## Claude desktop scheduled tasks (verified 2026-08-19)

- Local tasks (Claude app, Code tab, Routines, Local) run as real local
  Claude Code sessions: full user PATH, gh auth, git-lfs, uv, unrestricted
  network. File-backed at `~/.claude/scheduled-tasks/<name>/SKILL.md`
  (frontmatter + prompt body; schedule/folder/model live outside the file).
  Presets manual/hourly/daily/weekdays/weekly; finer cron by asking a
  desktop session. Fire only while the app is open and the machine awake;
  one catch-up run on wake covers the most recent miss (7-day window).
- Tasks pin to the creating session's launch folder — there is no folder
  parameter, and an in-session re-anchor does not change it.

## Claude Desktop MCP servers (verified 2026-08-30)

- The desktop app's server config is
  `~/Library/Application Support/Claude/claude_desktop_config.json` on
  macOS; a fresh install already carries `"mcpServers": {}` alongside
  unrelated app preferences, so writing a server entry is a merge into a
  live file, never a file create.
- GUI-spawned servers inherit launchd's default PATH
  (`/usr/bin:/bin:/usr/sbin:/sbin`) — `launchctl getenv PATH` is unset on
  a stock machine and Homebrew's `/opt/homebrew/bin` (where uv/uvx live)
  is absent. A server entry whose command chain needs uvx must carry an
  `env.PATH`, captured from the installing shell's PATH at write time.
- `.mcpb` extension bundles install under
  `~/Library/Application Support/Claude/Claude Extensions/` — separate
  from `mcpServers`; the two mechanisms coexist.
- Not yet verified in-app (pending first live use): whether server
  `instructions`, resources, and prompts all surface in desktop chat as
  the MCP spec suggests.

## Claude Code MCP servers (verified 2026-08-31)

- Claude Code does NOT read the desktop app's config. Its user-scope
  servers live at `~/.claude.json` (top-level `mcpServers`), relocatable
  with `CLAUDE_CONFIG_DIR`, which is also the only safe test seam. The
  Code tab inside the desktop app is Claude Code and reads that file too:
  sharing a process with the app buys nothing.
- `claude mcp add <name>` on a name that ALREADY EXISTS **refuses** — it
  never replaces: `MCP server <name> already exists in user config`, exit
  1. A writer that must replace an entry has to `claude mcp remove -s user
  <name>` first.
- `claude mcp remove <name>` on a name that is NOT THERE is also an error
  (`No MCP server named "<name>" in user scope`, exit 1). So a
  remove-then-add writer must ignore the remove's exit code, or every
  first-time write fails. Exit codes in full: remove-absent 1,
  remove-present 0, add-new 0, add-existing 1.
- Check these with `cmd >/dev/null 2>&1; echo $?` and not through a pipe —
  `cmd | tail; echo $?` reports the exit of `tail`, which is how the two
  facts above were first recorded backwards.
- `~/.claude.json` carries live session and project state (hundreds of
  project records beside `mcpServers`) and is rewritten by every running
  session — read it freely, but write it through `claude mcp`, or a
  concurrent session's write is silently lost.
- A user-scope server reaches NEW sessions only; an open one has already
  connected its servers.
- Claude Code inherits a real user PATH, unlike a GUI-spawned desktop
  server, so an entry written for it needs no `env.PATH` — freezing an
  install-day snapshot in would be a downgrade.

## Cowork (verified 2026-08-19)

- Cowork scheduled tasks are cloud unless a local folder is declared at
  creation (it can't be added later). "Run on your computer" there means an
  Ubuntu aarch64 VM with the folder mounted at the identical path: egress
  proxy allows github.com only (not raw.githubusercontent.com or any other
  subdomain), git/uv/ffmpeg/python present, no gh, no git-lfs, no root. Git
  over HTTPS to github.com works; enrichment fetches and the release-asset
  API are blocked.
- Cowork cloud containers have full network and git/git-lfs/uv/ffmpeg —
  the engine builds and runs there via uvx — but no gh; auth would need a
  stored per-instance PAT.
