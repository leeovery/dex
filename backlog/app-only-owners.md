# App-only owner surfaces

For an owner who lives in the Claude app,
two query-surface candidates to test: a Cowork session on the instance
folder (the sandbox's egress proxy allows github.com, so pull-first
freshness may work over an HTTPS remote with a token), or a claude.ai
Project with `wiki/` synced via the GitHub integration (read-only,
manual sync, token-capped). The local scheduled task keeps the folder
fresh either way. Cloud runners (Claude Code routines, Cowork cloud
tasks, Managed Agents) remain the path for owners with no computer —
they need a clone-first run mode and a stored per-instance PAT; not
built, not needed yet.

Since written, the better candidate has been designed:
`design/thin-query-surface.md` — a read surface for clients with no
filesystem, which is exactly what this owner is. Its v1 is local stdio and
does not reach them; this entry is now effectively the constituency for
that design's v2 (remote HTTP, GitHub OAuth, contents-API reads). The
Cowork and Project experiments above remain workarounds in the meantime.
