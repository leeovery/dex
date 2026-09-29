# Source indentation stored as indented code

A fetched article's markdown keeps the indentation its HTML source carried
in plain text, so prose can land with four or more leading spaces — and a
line like that opening a block reads to every markdown renderer as an
indented code block. No word is lost, but the page renders parts of itself
as code: a teaser or a paragraph shown as a listing.

Measured on 1,136 real pages through the article seam as of v0.2.8, counting
lines outside fences and list items that open with four or more spaces:
7,241 on 303 pages, of which 2,767, on 247 pages, follow a blank line —
where every renderer reads them as an indented code block. One long
interview transcript holds 1,479 of the 7,241.

Three shapes carry it, each seen on real pages:

- A paragraph hard-wrapped in the source keeps each wrapped line's
  indentation (an essay's `<p>` written across indented source lines; a
  transcript's intro).
- Whitespace between elements comes through as whitespace-only lines, and
  the next element's text opens after it at the source's indentation: a post
  index's `<h3>` and its `<p class="card-description">` teaser, twenty
  spaces deep.
- Code that reaches the output unfenced keeps its own indentation.

The second reading of pages whose body trafilatura cannot find (#208) makes
it worse where the marked body pulls in markup the first reading never
reached: 103 more lines after a blank line across the same 1,136 pages, the
most on two — addyosmani.com/blog (1 to 38, its teaser cards) and
thariqs.github.io/html-effectiveness (2 to 12; 8 to 42 counting every
indented line).

Not known yet: whether the whitespace comes through trafilatura's own
serialisation or survives from text nodes the page preparation leaves
alone, and so whether the repair belongs in `_prepare_page`, in a pass over
the extracted markdown, or upstream. Any repair has to leave real code
alone — an unfenced block's indentation is its content — and it changes
what lands for hundreds of pages, so it wants the same old-versus-new
comparison over real pages that every extraction change gets.
