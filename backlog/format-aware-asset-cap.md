# Format-aware asset cap — a PPTX is carousel-shaped

Extraction assets land under the tight cap (`MEDIA_MAX_FILES`, 4 per item)
whatever the document format, in document order — so a 30-slide deck keeps
its first four images and a DOCX keeps the letterhead while dropping the
chart. The media stage already answers this for kinds: `media_cap()` gives
the pooled 100-file backstop to kinds whose media IS the artefact
(Instagram carousels, X threads) and the tight cap to everything else. The
same admission test applies one level down, to formats: PPTX slides are
the post itself — one artefact split across images — while DOCX/XLSX
images are usually decoration around a body that carries the substance.

The shape of the fix: `_write_assets` consults the unit's format (already
on the ledger entry for file work) the way the download path consults
kind — PPTX pooled, the rest tight. Two costs to weigh before building
it: every landed asset is a describe obligation for the session, so the
cap also bounds per-item cognitive work; and document order means a raised
cap still fetches front-matter junk first for the formats that stay tight
— ordering is a separate judgment no cap can make.

Recorded alongside the pooled media cap, whose field trigger was a batch
of Instagram carousels truncated at four slides; the asset path kept the
tight cap deliberately, and this idea is the case for loosening it where
a format earns it.
