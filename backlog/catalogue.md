# The catalogue

A single mechanical artifact — `state/catalogue.json` — holding one line per
corpus item: id, date, title, url, sharer, topics, and the digest's opening
line. Emitted by the engine, no judgment involved. One read returns the whole
map of everything an instance knows.

The point is yield per call. Any client searching an instance — a thin query
surface, a browsable site, a resurfacing view — currently has to grep its way
toward the shape of the corpus before it can look for anything in it. The
catalogue hands over that shape in one go, and for a few hundred items it is
small enough to sit in context alongside the question.

**No embeddings and no BM25, for now.** The corpus fits in a context window
and `state/taxonomy.json` is already a curated routing table that beats
keyword ranking at "which topic is this". Adding a retrieval layer would be
building an index to rank results for a corpus that fits in one call. Revisit
around ten thousand items. When it does arrive, BM25 arrives as ranking over
grep results — a hundred dependency-free lines — never as a retrieval layer
underneath everything.

Syntheses are already an answer cache: a question that matches an existing
`wiki/syntheses/` page should surface it before anything else. Worth deciding
whether the catalogue covers syntheses as first-class entries or a search
consults them separately.

Open: where it is emitted (lint, the run's tail, or its own verb), whether it
is committed or rebuilt as cache — committed makes it readable through the
contents API without a clone, which is what a remote reader needs; cache
keeps it out of every diff.
