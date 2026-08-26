# Rewrite magnitude — a run that loses content says nothing about it

A re-fetch that replaces a stored enrichment file reports as `1 rewritten`,
whether the new body is twice the old one or half of it. The report has no
opinion on direction or size, so a rewrite that silently drops content
reads exactly like one that gains it.

This is the detector the silent-loss bugs of 0.1.0 needed and did not have.
Three of them shipped in that release — heading text eaten by permalink
anchors, fenced code blocks eaten by per-line anchors, a discussion page
reduced to its masthead — and every one was found by an owner diffing
rewritten files against git by hand, over cohorts of forty-odd files a
wave. Two of the three were caught only because someone happened to look.

There is a fourth case the fixes do not cover, and it is the one that makes
this worth building rather than filing as fixed: a metered host (Medium and
its network) can serve a partial render, and a re-fetch then legitimately
stores less than it had. Nothing is broken in the engine, the content is
genuinely gone from the response, and the run report cannot tell that apart
from an extractor regression — it says `1 rewritten` for both. The reporting
session's words: "the reportable half is that the run report cannot tell the
two apart and says nothing either way."

Filed from a session-observed report (#52) that named the mechanic
precisely: a stored file cut to 45% of its prose, reported as a plain
rewrite. It is not an engine contract violation — no documented behaviour
says the report states magnitude — which is why it is here rather than
fixed. It is a new reporting behaviour, and worth having.

## What it would take

The comparison is already in hand. `_write_output` byte-compares the
rendered file against what is on disk to decide whether to write at all, so
both bodies exist at the moment the decision is made; the shrink ratio is
one subtraction away. `_ItemOutcome` already carries `new` / `changed` /
`media` / `unchanged` counters and already renders a reason line.

## Open questions

- **What threshold, and measured against what.** Prose characters, not file
  bytes: frontmatter and a title change would swamp a small body. The
  reported case was 45%; a bar somewhere near "lost a third" is a guess
  until it runs against a real rerun cohort.
- **Where it renders.** Not `Needs writing up` — the item genuinely has new
  material and belongs there anyway. Probably its own section, on the model
  of `Already stored`: stated, not queued.
- **Whether it should do more than state it.** The owner's standing call
  across three waves was to leave the richer digest alone rather than
  revise it downward against a lossier source. If the report can name a
  shrink, the session can make that call deliberately instead of by
  noticing.
- **Whether the old body is recoverable at the point of report.** It is in
  git, and the enrichment file is committed, so a session can diff. Whether
  the engine should say so, or leave it, is a judgment call about how much
  the report should suggest.
