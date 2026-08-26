# HTML entities survive into enrichment text

Fetched text is stored with its HTML entities undecoded, so a transcript holds
literal `&nbsp;` between words rather than a space. Measured in dex-engineering
on 2026-08-26: **17,724 `&nbsp;` occurrences**, concentrated in 34 of 758
youtube enrichment files, plus 13,727 `&gt;`, 3,729 `&lt;`, 234 `&amp;` and a
tail of `&mdash;`, `&rarr;`, `&bull;`, `&middot;` across all kinds.

The consequence is a silent recall failure, not a display problem. Any
mechanical text search over enrichment — the dex-query recall sweep, a grep for
a remembered phrase, a citation check — fails on **multi-word phrases** in an
affected file, because `jagged&nbsp;intelligence` does not match "jagged
intelligence" and nothing about the miss is visible. Single words still match,
so the failure is partial and looks like the phrase was never there.

Found while calibrating a scorer against these files: a quote-traceability check
reported the frontier-written digests as unverifiable on most items, and the
cause was entirely this.

To fix: decode entities once, at the point fetched text becomes an enrichment
file, so the stored text is the text. Candidates for that point are the
enrichment-file format seam and the drivers that produce body text. Worth
checking whether the caption path and the article-extraction path both need it,
or whether one already decodes. Existing instances need a backfill pass or a
migration — the affected files are already committed.
