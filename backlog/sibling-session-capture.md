# Sibling sessions feeding dex

Claude sessions working in OTHER projects see capture-worthy things
(links, decisions, references) and currently have no sanctioned way to
consult dex or suggest a capture. An early mechanism existed: a paragraph
in the owner's global `~/.claude/CLAUDE.md` steering every session to
consult dex-engineering read-only and suggest captures into its `inbox/`.
The owner removed it (2026-08-22, confirmed 2026-08-25) as hastily added;
this entry replaces it with a design conversation to have properly.

To design: the carrier (a global CLAUDE.md paragraph again, a shared
skill, or something instance-declared), the scope (which instances a
sibling session may see, and how it picks between them), the boundary
(read-only consultation vs writing capture files vs merely suggesting to
the owner), and how the capture keeps provenance when it arrives from a
session with no dex context. Whatever the shape, it must not make every
unrelated session pay a context tax for dex's benefit.

The carrier is now built, not just designed: `design/thin-query-surface.md`'s
local MCP server, filed at user scope by `bin/dex connect --client code`
(§11). Every Claude Code session on the machine can reach the owner's dexes,
and it costs an unrelated session no standing context — nothing is read until
a tool is called, which is this entry's own hard constraint.

What remains here is the residue the carrier does not answer: provenance on a
capture arriving from a session with no dex context, whether some projects
should be excluded from a machine-wide server, and whether a CLAUDE.md nudge
is still wanted on top of a server that no session will think to call.
