# The owner's curation surface

Correcting a dex requires a terminal. Pinning a claim, excluding an item,
changing what an instance covers, saying "no, that's wrong" — all of it runs
through a Claude Code session on the folder. An owner who does not have one
cannot correct their own knowledge base.

That is the sharpest gap in the system, because the deal offered to an owner
is that their job is taste. Taste with no way to act on it is a spectator
seat: they can watch the wiki be wrong.

To design: which corrections are worth exposing (a pin, an exclusion, a scope
change, a "this page is wrong about X" note the next run must reconcile), and
what carries them. A thin client cannot hand-edit `wiki/pins.md` — state is
written through verbs precisely so a malformed record is impossible, and that
must hold no matter how thin the client is. The likely shape is that a
correction is itself a capture: a note landing in the inbox, picked up by the
next run, applied through the existing verbs by the session that already has
judgment loaded.

Related: `thin-query-surface.md` is the same problem for reads, and whatever
carries queries probably carries corrections.
