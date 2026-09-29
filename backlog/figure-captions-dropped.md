# Figure captions are dropped from every article

A fetched article's `<figure>` elements never reach the stored markdown,
captions included: trafilatura deletes every figure when image extraction is
off, which it is for every page, so a `<figcaption>` saying what a chart or
diagram shows is lost even on a page with a proper `<article>` body. The
text of a figure is often where a post states its result ("Figure 3: cost
fell 40% at equal accuracy"), and nothing else on the page repeats it.

Seen on a blog post whose nine captioned figures carried the eval results
the prose referred to (#208, whose headings and lists were fixed without
this). Reproduced on a synthetic page: an `<article>` with a captioned
figure between two paragraphs extracts both paragraphs and no caption.

Two routes are open, neither measured: turn image extraction on and strip
the image lines it adds, or carry each `<figcaption>`'s text out of its
figure during page preparation. Either one changes what lands for every
page with a figure, not only the pages #208's second reading touches, so
picking this up means the old-vs-new comparison over real pages that every
extraction change gets.
