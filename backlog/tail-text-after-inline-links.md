# Extraction loses tail text after inline links in list items

One observed page — platform.claude.com's Opus 5 prompting guide — loses
real content in extraction: the text that follows an inline `<a>` inside a
`<li>` (`…<a href="/docs/…">effort</a> produce strong quality at a
fraction of the tokens…`) vanishes from the extracted markdown, and two
list items merge into one line. The bytes are in the fetched HTML; the
loss happens inside trafilatura, not in the fetch or the page
preparation.

Observed in the dex-engineering wave that filed engine issues #70–#75,
recorded in that instance's `state/issue-reports.jsonl` under
fp-4228eaa5f20c as "possibly the same root cause" as the emptied
headings. It is not: the glyph-anchor repair that fixed the headings does
not recover this text, and a minimal reconstruction of the shape (link
tail inside `<li>`, with and without sibling `<code>` elements) extracts
cleanly — the loss needs something else in the real page's structure that
has not been isolated yet.

This is genuine content loss, not markup noise, which is what makes it
worth holding: the fix starts with bisecting the live page's HTML down to
the smallest fragment that still drops the tail, then deciding whether it
is a `_prepare_page` repair or a trafilatura issue to report upstream.
The saved page is large (~477KB) and app-shell-heavy, so expect the
trigger to be a wrapper element rather than the visible list itself.
