# PDF figures are never extracted — the text survives, every image is lost

PDF extraction carries no assets at all: anydoc has no document-model form
for PDF and converts it straight to markdown, so `_assets` returns an
empty list for `Format.PDF` before any cap can apply (sanctioned by the
extractor contract — populate assets or return none). A chart-heavy
report, a lookbook, a linesheet, a deck exported to PDF: the body text
lands, and every figure — often the substance of exactly those documents —
silently does not. No ledger line records the loss, so no later stage can
see it.

The fix is an extraction capability, not a constant: either an extractor
that reads embedded images out of the PDF, or page renders (rasterize the
pages and route them as assets, which suits the lookbook shape where the
page IS the image). Both feed the existing describe obligation, so the
per-item cognitive cost bounds how greedy either should be. Whichever
lands, the tight 4-file asset cap would immediately re-truncate an
image-heavy document — [the format-aware asset cap](format-aware-asset-cap.md)
is the other half of this idea.

Recorded alongside the pooled media cap for carousels and threads, when
the asset path's caps were checked for the same truncation and the PDF
answer turned out to be "nothing to truncate — the figures never arrive".
