# A browsable wiki

There is no surface for looking at a dex without asking it a question.
Everything today is question-shaped: open a session, ask, get a cited answer.
Browsing — what came in this week, what is in here, what did I save last
month, follow a link and see where it goes — has no home at all, and it is
the mode that makes a knowledge base feel alive rather than write-only.

The pages themselves do not need rewriting. They are written for an agent to
re-read and that is correct; the corpus is truth and the wiki is a build
artifact. What is missing is a *render* of them, which the engine already has
a seam for.

Shape: a static build from wiki, digests and corpus. Home leads with recent
activity and the topic map. Item pages carry the source link, the owner's
verbatim note, the digest, and the fetched material behind it. Backlinks
between pages. Client-side full-text search over the built output. Published
somewhere private — a static host behind access control, which costs nothing
and works on a phone.

`bin/dex serve` covers the owner who wants nothing published.

Worth saying out loud to anyone waiting on this: opening the instance folder
in Obsidian works today, with zero engineering. Wikilinks and markdown are
already the format. It is not the answer, but it is the answer until this
exists.

Feeds on `catalogue.md`. Delivers the views described in
`resurfacing-reading-queue.md`.
