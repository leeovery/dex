# Page screenshots: a rendered capture of the page's look

A lens that reads for how things look, a design lens above all, gets a
web page's text and almost never its look. The web driver lands the
extracted article and, when the page declares one, its `og:image`, so a
design instance sees a site's look only when an image happens to come
down: a social card, or a screenshot the owner attached to the share. The
lens verdict already treats that as a capture gap rather than a drop, and
the digest works from what landed and the owner's note, but the item never
holds what the owner shared it for. The owner's use is recalling nice
designs by their style, asking for "the one with the oversized serif and
the muted greens", not only by the site's name, and a text-only item
cannot answer that.

The shape it would take: a rendered-page capture, a screenshot stored as
an enrichment media unit beside the page's text, described through
`bin/dex enrich item describe` like any other media file (the description
is where style, palette, composition, typography and layout get written
down, which is what makes the look searchable), and held in LFS like the
rest of the item's media. The describe queue, the digest's `media:`
listing and the health check's describe row all count it without change,
because it is one more media file the item carries.

A fast follow to the lens release: the lens made reading for the look a
first-class judgment, and this is the input that judgment is missing.

Open questions:

- Which layer captures it: the web driver taking a screenshot as part of
  its fetch, or a capability (like transcribe or extract) with a provider
  order in config, so an instance without a browser falls back to nothing
  and says so.
- A headless browser's cost and its dependency weight: Playwright or a
  Chromium download is heavy beside the engine's other dependencies, runs
  slower than a plain fetch, and meets the same bot walls the text fetch
  already hits.
- Viewport or full page: a viewport shot at a fixed width is small and
  shows the page as a visitor first sees it, while a full-page capture
  shows the whole layout at a size the describe step and LFS both pay for.
- Whether every web page gets one or only the pages the lens asks for,
  which would put a judgment (does this lens read for the look?) in front
  of a mechanical fetch, perhaps as a harvest-time promotion the session
  makes for a design lens.
