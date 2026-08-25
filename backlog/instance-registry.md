# Joining instances — the registry

An owner with five instances (engineering, design, marketing, health) has no
way to ask one question across them. Each is independent by intent — the
knowledge must not interweave — but "search the dex" should reach all of them
unless a specific one is named.

The engine cannot hold this. It has no knowledge of any specific instance and
that separation is load-bearing. So the registry is owned by the person: a
list of instance roots or `owner/repo` pairs, read by whatever fans out —
a query server, a site, a capture client.

Routing needs no model call. Every instance already declares its scope in its
own CLAUDE.md, so a registry that reads those bullets can send a question to
the instances whose scope covers it, or to all of them, and the answer cites
per-instance item ids that stay distinguishable by repo.

Note this problem is already solved once, badly-scoped: the phone shortcut
carries a dictionary of instances so it can offer a picker. That is the same
registry. Design these together, or let one absorb the other — see
`shortcut-per-instance-tokens.md`, which is about the token half of the same
dictionary.

Open: where the registry lives (a dotfile in the owner's home, a config in
whatever consumes it, or discovered by scanning a dex home directory), and
whether an instance can decline to be reachable from a shared surface.
