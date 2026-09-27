"""The shared article-fetch seam: fetch a page, extract it, rescue via wayback.

Two drivers read pages as articles — the web catch-all for everything it
matches, and the paper driver for the non-arxiv hosts (openreview,
hf-papers) whose pages read fine as articles — and the whole route is one
behaviour: classified fetch, mid-fetch re-detection of bodies that are not
web content at all, trafilatura extraction with links kept, thin-extraction
parking, and the wayback fallback for failed fetches. It lives here, beside
:mod:`dex_engine.drivers.transport` and the other shared seams, because a
seam several drivers share is not itself a driver, and a driver must never
import another driver.

Fetching and extraction are split on purpose: ``trafilatura.fetch_url``'s
failure mode — ``None`` for everything — is what caused the motivating
incident. The transport fetches with visible status codes; trafilatura is
demoted to extraction only, with ``include_links=True`` because the old
``include_links=False`` stripped the very URLs harvest reasons over.

That flag has a price, and the page is prepared before extraction to pay
it: with links on, an anchor carrying no text of its own does not merely
render as nothing, it swallows the content beside it — heading words after
a permalink, whole fenced blocks behind mkdocs-material's per-line
anchors. The same permalink wearing a glyph for its text is not empty,
so it escapes that test and fails both ways: swallowing the heading it
sits in, or leaking its glyph into the heading line. Extraction therefore
runs twice over the prepared page, once without comments and once with,
because the two settings answer different questions: an article's comment
section is chrome, and a discussion page's comments are the entire
artifact. The same preparation repairs markup trafilatura throws away
with content inside it: scroll-area wrappers its navigation filter
misreads, code blocks whose line breaks live in the stylesheet, MathML,
and the LaTeXML shapes of an arXiv rendering. The arXiv full text in the
paper driver runs through it too.

A page can make extraction unnecessary: docs sites declare the markdown
a page was rendered from (``<link rel="alternate" type="text/markdown">``),
and that source keeps the tables and code blocks extraction loses. The
route fetches it exactly where the page says — never a guessed ``.md``
URL, because GitHub's points at an API path and Fern's at another slug —
and stores it whenever it answers as markdown or plain text and carries
at least what extraction found: its length, and its fenced blocks and
table rows. The title, description and og:image still come from the
HTML.

Wayback fallback stays for failed fetches, and its failures are classified
like any fetch, never swallowed. A 200 whose extraction comes back thin is
``Unusable`` (evidence ``thin-extraction``), never ``Missing`` — our
tooling can't read it; that doesn't mean it's gone.

A 200 whose BODY is a document (magic bytes say PDF/OOXML/…, or the
content type maps to an extractable format) is neither thin nor article
work at all: detection's HEAD lied or was inconclusive. The route signals a
redetection to ``file`` work and the run layer re-routes the unit — never
``manual`` for content the file driver can read. A body whose bytes are a
picture, a video or an audio file redetects the same way, to the media
stage: the image an owner shared belongs in the item's media, and an
article driver has nothing to extract from it.

The same mid-fetch discovery carries indie podcast episode pages to the
``podcast`` driver, decided on content rather than on URL patterns that
cannot tell ``/feed`` the blog from ``/feed`` the show. What counts as
an episode signal is narrow, because the two mistakes cost differently:
handing an article to the podcast driver parks it awaiting transcription
with an EMPTY body — the article is never extracted and its links are
never harvested — while handing an episode page to ``web`` merely stores
its show notes. So an episode is a page whose ``og:audio`` names the audio as the
page's own object, or one carrying a player and no body worth extracting.
An ``<audio>`` element beside a real article is a read-aloud widget or a
media sample, and the article wins.
"""

import html as html_lib
import json
import re
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lxml.html import HtmlElement

from dex_engine.pipeline.classify import (
    MIN_SUBSTANTIAL_CHARS,
    THIN_EXTRACTION_REASON,
    Classification,
)
from dex_engine.pipeline.detect import (
    CONTENT_TYPE_FORMATS,
    sniff_document,
    sniff_format,
    sniff_media_ext,
)
from dex_engine.pipeline.types import Content, Format, Job, Kind, Outcome, Redetected, Unusable

from .audio import audio_enclosure
from .fetch import FetchFailure, fetch_classified
from .transport import HttpResponse, Transport

__all__ = ["HtmlExtract", "fetch_article", "trafilatura_extract"]

# html -> markdown, or None when nothing extractable was found.
HtmlExtract = Callable[[str], str | None]

_WAYBACK_AVAILABLE = "https://archive.org/wayback/available?url="

# The captured value runs from the opening quote to the closing one and may
# not cross a line break: a wrapped content attribute has no og:image this
# seam will vouch for. Capturing up to the break instead yielded the
# truncated head of the URL ("https://cdn.example.test/"), which is a
# perfectly well-formed request for a resource that does not exist — a
# guaranteed junk fetch ledgered as a real media unit.
_OG_IMAGE_RES = (
    re.compile(
        r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+content=[\"']([^\"'\r\n]+)[\"']"
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"'\r\n]+)[\"'][^>]+(?:property|name)=[\"']og:image[\"']"
    ),
)
# A discussion page's comments ARE the artifact; an article's are chrome,
# and dropping them is right for the article. Nothing in the markup tells
# the two apart, but the SIZE does, and not marginally: measured over docs
# sites, blogs, a news post and a Django reference, every article extracts
# to exactly the same length either way (ratio 1.00), while a Hacker News
# thread grows 38x — 1,058 characters of masthead against the 40,353 the
# 115 comments actually are. There is nothing in between to misjudge, so
# the bar sits far from both: a page whose comments more than double it is
# a discussion, and the discussion is what was shared.
_DISCUSSION_RATIO = 2.0

# What a heading's permalink puts on screen when it puts anything: the
# hash is the one the field reported, and the pilcrow and the section
# sign are the other two characters the same chrome is drawn with.
_PERMALINK_GLYPHS = frozenset({"#", "¶", "§"})

_WHITESPACE_RUN_RE = re.compile(r"\s+")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_TITLE_CHARS = 200

# A standfirst — the one-sentence deck under the headline — is markup the
# page HEADER carries, not the article: Substack puts it in `post-header`
# beside the H1, trafilatura reads that whole block as boilerplate, and the
# deck leaves the extraction with it. The publisher declares the same
# sentence here, where extraction cannot reach it. og: leads because a page
# writing both puts its considered wording there, and the bare
# `description` is what a page with no og: block has. The value may span
# newlines, unlike og:image above: a wrapped attribute is prose to collapse,
# not a truncated URL to refuse.
_DESCRIPTION_RES = (
    re.compile(
        r"<meta[^>]+(?:property|name)=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']"
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:description[\"']"
    ),
    re.compile(
        r"<meta[^>]+(?:property|name)=[\"']description[\"'][^>]+content=[\"']([^\"']+)[\"']"
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']description[\"']"
    ),
)
_MAX_DESCRIPTION_CHARS = 300

# A page's declaration of the markdown it was rendered from. Its attributes
# come in any order and either quoting, so the tag is found first and read
# attribute by attribute.
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE_RE = re.compile(r"""([^\s"'<>/=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>]+))""")
_MARKDOWN_TYPE = "text/markdown"
_MARKDOWN_ANSWER_TYPES = frozenset({_MARKDOWN_TYPE, "text/x-markdown", "text/plain"})
_FEED_LEADS = (b"<rss", b"<feed")
# The structure a declared source is held to, counted the same way on both
# sides: a fence line opens or closes a block, and a table row opens with
# its pipe.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^[ \t]*\|", re.MULTILINE)

_CODE_LINE_CLASS = "line"

# HTML's phrasing content: what a run of text beside a code block is made of.
_PHRASING_TAGS = frozenset(
    {
        "a", "abbr", "audio", "b", "bdi", "bdo", "br", "button", "canvas", "cite", "code",
        "data", "del", "dfn", "em", "embed", "i", "iframe", "img", "input", "ins", "kbd",
        "label", "mark", "math", "meter", "object", "output", "picture", "progress", "q",
        "ruby", "s", "samp", "select", "small", "span", "strong", "sub", "sup", "svg",
        "textarea", "time", "u", "var", "video", "wbr",
    }
)  # fmt: skip
_LIST_ITEM_TAGS = ("li", "dt", "dd")

_LATEXML_TABLE_SECTIONS = {"ltx_thead": "thead", "ltx_tbody": "tbody", "ltx_tfoot": "tfoot"}

_TEX_ANNOTATION_TEXT = ".//annotation[@encoding='application/x-tex']//text()"
_RENDERED_MATH_TEXT = ".//text()[not(ancestor::annotation or ancestor::annotation-xml)]"


def trafilatura_extract(html: str) -> str | None:
    """Extract markdown from HTML via trafilatura — extraction ONLY.

    ``include_links=True`` is load-bearing: harvest reads the preserved
    hyperlinks. It is also, unaccompanied, destructive — see
    :func:`_prepare_page`, which is why the page is prepared first and
    never handed to the extractor raw.
    """
    prepared = _prepare_page(html)
    body = _extract_markdown(prepared, comments=False)
    discussion = _extract_markdown(prepared, comments=True)
    if discussion is not None and len(discussion) > len(body or "") * _DISCUSSION_RATIO:
        return discussion
    return body


def _extract_markdown(html: str, *, comments: bool) -> str | None:
    import trafilatura  # noqa: PLC0415 — lazy: heavy dep, loaded only when extracting

    return trafilatura.extract(
        html, output_format="markdown", include_links=True, include_comments=comments
    )


def _prepare_page(html: str) -> str:
    """Repair the page shapes that break extraction, before trafilatura sees it.

    Every repair runs over one parse, and each function below carries the
    field case that earned it. They come in two families: what
    ``include_links`` costs — empty anchors, and permalink glyphs and
    line breaks in headings — and markup trafilatura throws away with
    content inside it — scroll areas its navigation filter misreads, code
    lines whose breaks live in the stylesheet, MathML, and LaTeXML's
    tabulars and equation tables.

    A page lxml cannot parse goes through untouched: preparation is a
    repair, never a gate.
    """
    from lxml.etree import LxmlError  # noqa: PLC0415 — lazy: pulled in with trafilatura
    from lxml.html import HtmlElement, fromstring, tostring  # noqa: PLC0415

    try:
        tree = fromstring(html)
    except (LxmlError, ValueError):
        return html
    _unmask_scroll_areas(tree)
    _break_code_lines(tree)
    _tabulate_latexml_spans(tree)
    # Before the rest of the math: an equation row's formulas are one
    # display block, and rendered one element at a time they would come
    # back as separate inline ones.
    _flatten_equation_tables(tree)
    _render_math(tree)
    _drop_empty_anchors(tree)
    for heading in tree.iter("h1", "h2", "h3", "h4", "h5", "h6"):
        if isinstance(heading, HtmlElement):
            _drop_glyph_anchors(heading)
            _flatten_heading(heading)
    # Last: it sorts a container's children into text and blocks, so it must
    # read the markup every repair above leaves, the math already text.
    _set_code_blocks_apart(tree)
    return tostring(tree, encoding="unicode")


def _drop_empty_anchors(tree: "HtmlElement") -> None:
    """Drop every anchor carrying no text — permalinks, icon links, line anchors.

    An anchor with nothing inside it is chrome, and with ``include_links``
    on it is chrome that eats content: a heading whose permalink precedes
    its words (``<h2><a class="headerlink" href="#x"></a>Text</h2>`` — the
    Sphinx/mkdocs/Docusaurus spelling) extracts as a bare ``##`` with the
    words gone, and mkdocs-material's per-line code anchors take whole
    fenced blocks with them (a llama-cpp docs page: 42 fences down to
    none). Dropping them is lossless — there is nothing inside an empty
    anchor to lose, and an anchor with no text renders no link. On that
    same page this recovers 92 fences AND keeps all 42 hyperlinks, which
    neither link setting manages alone.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    # Materialized before the loop: drop_tree() detaches from the live tree,
    # and mutating what you are iterating skips siblings. drop_tree is also
    # the right removal — it MERGES the anchor's tail into the preceding
    # text, which is exactly where a heading's words live.
    for anchor in list(tree.iter("a")):
        if isinstance(anchor, HtmlElement) and not (anchor.text_content() or "").strip():
            anchor.drop_tree()


def _drop_glyph_anchors(heading: "HtmlElement") -> None:
    """Inside a heading, drop the anchors whose text is a bare permalink glyph.

    They are the empty anchor's chrome with an icon character for
    content, non-empty enough to survive that drop and broken both ways.
    Wrapped around a span before the words they swallow exactly as an
    empty anchor does — a cursor.com post came back with every heading a
    bare ``##`` — and bare they leak the glyph into the heading line
    instead, ahead of the words on laravel-news.com and behind them on a
    hugo blog whose anchor trails its heading. Restricting this to
    headings is what makes it safe: a link reading ``#`` in body prose is
    not provably chrome, and one inside a heading is.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for anchor in list(heading.iter("a")):
        if not isinstance(anchor, HtmlElement):
            continue
        if anchor.text_content().strip() in _PERMALINK_GLYPHS:
            anchor.drop_tree()


def _flatten_heading(heading: "HtmlElement") -> None:
    """Put a heading on one line: each ``<br>`` a space, whitespace runs collapsed.

    A markdown heading is one line by construction, and a heading holding
    a line break (``<h3><span>Use Case: <br></span></h3>`` — Hugging Face
    model cards) extracts as the marker alone on its line with the words
    below it, among the span's source-formatting tabs and newlines emitted
    verbatim — an empty heading to any consumer keying on the heading
    line. The ``<br>`` is the break; the whitespace collapse is what puts
    the words beside the marker cleanly instead of behind a run of tabs.
    Its replacement is a space and not nothing, because a break BETWEEN
    two parts is the only separator they have: a page whose top heading
    carries its subtitle after a ``<br>`` folded to one welded line, the
    subtitle no longer a line at all and two words run together.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for br in list(heading.iter("br")):
        if isinstance(br, HtmlElement):
            # A space stands in for the break before the element goes:
            # drop_tree() merges the tail into the preceding text with
            # NOTHING between, and the <br> is the only word boundary
            # there is. Deleting it outright ran the parts together —
            # "The Big HeadingA one-line subtitle" — which loses the
            # subtitle as a line AND welds two words into one. The
            # whitespace collapse below folds whatever doubling this
            # creates against padding already there.
            br.tail = f" {br.tail or ''}"
            br.drop_tree()
    for node in heading.iter():
        if not isinstance(node, HtmlElement):
            continue
        if node.text:
            node.text = _WHITESPACE_RUN_RE.sub(" ", node.text)
        # The heading's own tail is the text AFTER it — not its content.
        if node is not heading and node.tail:
            node.tail = _WHITESPACE_RUN_RE.sub(" ", node.tail)


def _unmask_scroll_areas(tree: "HtmlElement") -> None:
    """Drop the class tokens that style a scrollbar.

    trafilatura discards any div, section, paragraph or span whose class
    attribute contains ``bar`` anywhere — its spelling of navbar and
    sidebar — and a scroll area's scrollbar styling matches it too.
    Mintlify wraps every table and code block in one
    (``base-ui-disable-scrollbar``), and its docs pages came back as prose
    alone, every table row and code fence gone. Styling never decides
    extraction, so those tokens go and every other token stays: an element
    that really is navigation still says so in the rest of its classes.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for element in tree.iter():
        if isinstance(element, HtmlElement) and "scrollbar" in _class_attribute(element).lower():
            kept = [token for token in _classes(element) if "scrollbar" not in token.lower()]
            element.set("class", " ".join(kept))


def _break_code_lines(tree: "HtmlElement") -> None:
    """Put a newline between the lines of a code block typeset one element per line.

    Shiki wraps each line of a block in ``<span class="line">``, and a page
    that renders those spans as components, rather than as Shiki's own
    string, leaves nothing between them: the breaks live in the stylesheet,
    which draws each span as a block. trafilatura reads text, so the block
    came out as one line, and one line of code is inline code — every block
    of a towardsdatascience.com post was stored as a single backticked line,
    its statements run together (``from PyPDF2 import PdfReaderimport
    nltk``). vercel.com's blog draws its lines as ``<div class="line">``
    the same way.

    A line that already ends in a newline keeps what it has: Shiki's string
    output puts one between its spans, and Hugo's Chroma ends each span
    with one. A block's last line keeps what it has too: a break after it
    adds no line, and it would turn a one-line block into a fenced one.
    """
    for line in _code_lines(tree):
        if line.getnext() is not None and not _ends_its_line(line):
            line.tail = f"\n{line.tail or ''}"


def _code_lines(tree: "HtmlElement") -> "Iterator[HtmlElement]":
    """Every line element of a code block: a ``line``-classed child of its ``<code>``.

    A child and never a deeper descendant, because deeper in, the class
    names a token rather than a line: Prism's diff highlighting calls the
    changed text inside each line element ``line``.
    """
    for pre in tree.iter("pre"):
        for code in pre.iterchildren("code"):
            yield from (line for line in code if _CODE_LINE_CLASS in _classes(line))


def _ends_its_line(line: "HtmlElement") -> bool:
    return line.text_content().endswith("\n") or (line.tail or "").startswith("\n")


def _set_code_blocks_apart(tree: "HtmlElement") -> None:
    """Give every code block a line of its own, and keep the text around it.

    trafilatura starts a paragraph or a heading on a new line, but not a
    code block, and it drops text left loose after one. So a block written
    among loose text came out glued. MkDocs keeps a ``<details>`` section's
    prose as bare text around each highlighted block, and
    llama-cpp-python's install page was stored with thirteen fences opening
    at the end of the sentence before them (``saved to a `requirements.txt`
    file:`` then the fence), every one read inside out by a renderer, and
    the prose between the blocks gone. The loose text beside a code block
    becomes paragraphs, which trafilatura sets apart from the block and
    keeps.

    A list item needs one more repair: trafilatura writes an item's
    paragraphs on a single line, the fence with them, so a line break goes
    in front of the block. It carries a space after it, because trafilatura
    drops a break with nothing after it, and the fence line opens with that
    space, which markdown allows. A definition list's terms and
    descriptions are list items to trafilatura too.
    """
    for pre in list(tree.iter("pre")):
        for block, container in _code_block_levels(pre):
            _paragraph_loose_text(container)
            if next(block.iterancestors(*_LIST_ITEM_TAGS), None) is not None:
                _break_line_before(block)


def _code_block_levels(pre: "HtmlElement") -> "Iterator[tuple[HtmlElement, HtmlElement]]":
    """Each level from the pre out, as the block and its container, up to where its text is.

    Highlighters wrap a block in divs, with copy buttons and a filename
    beside it, so the text it sits among may be several divs out: the walk
    ends at the first container that holds loose text or is not a div.
    """
    block = pre
    while (container := block.getparent()) is not None:
        last = container.tag != "div" or _holds_loose_text(container)
        yield block, container
        if last:
            return
        block = container


def _paragraph_loose_text(container: "HtmlElement | None") -> None:
    """Wrap each run of loose text in the container, with the phrasing inside it, in a ``<p>``.

    A run holding no loose text of its own stays as it is: a link alone on
    its line survives extraction, and the same link made a paragraph is
    dropped as boilerplate.
    """
    if container is None or not _holds_loose_text(container):
        return
    previous: HtmlElement | None = None
    lead = container.text
    container.text = None
    run: list[HtmlElement] = []
    for child in list(container):
        if _is_phrasing(child):
            run.append(child)
            continue
        _close_run(container, previous, lead, run, before=child)
        previous, lead, run = child, child.tail, []
        child.tail = None
    _close_run(container, previous, lead, run, before=None)


def _close_run(
    container: "HtmlElement",
    previous: "HtmlElement | None",
    lead: str | None,
    run: list["HtmlElement"],
    *,
    before: "HtmlElement | None",
) -> None:
    """Wrap one run in a ``<p>`` where ``before`` stands, or put its lead text back."""
    from lxml.html import Element  # noqa: PLC0415 — lazy: pulled in with trafilatura

    if not _worth_a_paragraph(lead, run):
        if previous is None:
            container.text = lead
        else:
            previous.tail = lead
        return
    paragraph = Element("p")
    paragraph.text = lead
    paragraph.extend(run)
    if before is None:
        container.append(paragraph)
    else:
        before.addprevious(paragraph)


def _worth_a_paragraph(lead: str | None, run: list["HtmlElement"]) -> bool:
    """Whether a run's own text outweighs its links, so that made a paragraph it is kept.

    trafilatura drops a paragraph made mostly of links as boilerplate, and
    a footnote's marker and URL after a code block are one — a Hacker News
    comment's ``[1] https://arxiv.org/pdf/...`` was lost that way. Left loose
    it is kept, as a link alone on its line is.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    loose = sum(len((text or "").strip()) for text in (lead, *(node.tail for node in run)))
    linked = sum(
        len(link.text_content().strip())
        for node in run
        if isinstance(node, HtmlElement)
        for link in node.iter("a")
    )
    return loose > linked


def _holds_loose_text(container: "HtmlElement") -> bool:
    return _any_text(container.text, *(child.tail for child in container))


def _any_text(*texts: str | None) -> bool:
    return any((text or "").strip() for text in texts)


def _is_phrasing(node: "HtmlElement") -> bool:
    """Whether a node belongs to a run of text: a comment, or phrasing holding no code block."""
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    if not isinstance(node, HtmlElement):
        return True
    return node.tag in _PHRASING_TAGS and next(node.iter("pre"), None) is None


def _break_line_before(block: "HtmlElement") -> None:
    from lxml.html import Element, HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    siblings = block.itersiblings(preceding=True)
    previous = next((node for node in siblings if isinstance(node, HtmlElement)), None)
    if previous is None or not previous.text_content().strip():
        return
    line_break = Element("br")
    line_break.tail = " "
    block.addprevious(line_break)


def _tabulate_latexml_spans(tree: "HtmlElement") -> None:
    """Rebuild LaTeXML's span-typeset tabulars as the tables they draw.

    LaTeXML typesets a tabular sitting inside running text — a table
    scaled to fit, a header cell stacked over two lines — as spans:
    ``ltx_tabular`` over ``ltx_tr`` over ``ltx_td``, with head and body
    wrappers between. To an extractor that is inline text with no rows in
    it, and worse inside the ``<figure>`` LaTeXML floats a table in:
    trafilatura deletes every figure that holds no real ``<table>``. An
    appendix of nothing but such tables was left a bare heading, and bare
    headings after the references are what trafilatura strips from a
    page's end — two of one paper's three appendices vanished whole. Once
    rebuilt, the table is real and trafilatura keeps its figure itself.

    The spans the tabular sits in become blocks with it: a table left
    inside inline markup is read as its paragraph's text, one cell to a
    line and no rows. Only an outermost tabular is rebuilt: one inside a
    table cell is that cell's own layout ("Msg-Wise" stacked over
    "(%, ↑)"), and a table inside a cell has no markdown form.
    """
    for tabular in list(tree.iter("span")):
        if "ltx_tabular" in _classes(tabular) and not _in_table_cell(tabular):
            _as_table(tabular)


def _in_table_cell(element: "HtmlElement") -> bool:
    return any(ancestor.tag in ("td", "th") for ancestor in element.iterancestors())


def _as_table(tabular: "HtmlElement") -> None:
    tabular.tag = "table"
    for ancestor in tabular.iterancestors():
        if ancestor.tag != "span":
            break
        ancestor.tag = "div"
    rows = []
    for child in tabular:
        section = _table_section(child)
        if section is None:
            rows.append(child)
        else:
            child.tag = section
            rows.extend(child)
    for row in rows:
        if "ltx_tr" in _classes(row):
            row.tag = "tr"
            for cell in row:
                # A header cell stays a td: LaTeXML's own <table> rendering
                # writes one, and a paper's tables read alike in both forms.
                if "ltx_td" in _classes(cell):
                    cell.tag = "td"


def _table_section(element: "HtmlElement") -> str | None:
    """The table section a LaTeXML head/body/foot wrapper stands for, or None."""
    for token in _classes(element):
        if token in _LATEXML_TABLE_SECTIONS:
            return _LATEXML_TABLE_SECTIONS[token]
    return None


def _flatten_equation_tables(tree: "HtmlElement") -> None:
    """Rewrite LaTeXML's equation tables as one display-math line per row.

    LaTeXML lays out a numbered equation as a table row — padding cells,
    the formula, the equation number — and with the math deleted,
    trafilatura kept the shell: rows of empty cells like
    ``|  |  |  | (1) |``. A row's formulas are one display block even where
    an aligned equation splits them across cells, so they join into one
    ``$$`` block, followed by whatever else the row says: its number,
    which the prose cites.
    """
    from lxml.html import Element  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for table in list(tree.iter("table")):
        parent = table.getparent()
        if parent is None or "ltx_eqn_table" not in _classes(table):
            continue
        lines = Element("div")
        for row in table.iter("tr"):
            line = _equation_line(row)
            if line:
                paragraph = Element("p")
                paragraph.text = line
                lines.append(paragraph)
        lines.tail = table.tail
        parent.replace(table, lines)


def _equation_line(row: "HtmlElement") -> str:
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    formulas = [math for math in row.iter("math") if isinstance(math, HtmlElement)]
    formula = " ".join(source for source in map(_math_source, formulas) if source)
    for math in formulas:
        math.drop_tree()
    parts = (f"$${formula}$$" if formula else "", row.text_content().strip())
    return " ".join(part for part in parts if part)


def _render_math(tree: "HtmlElement") -> None:
    """Replace every MathML element with its formula between dollar signs.

    trafilatura deletes ``<math>`` outright and closes the text over the
    gap: an arXiv paper's "an average gain of +17.8 points" was stored as
    "an average gain of  points", and every delta in its ablation study
    went the same way. Display math (``display="block"``) becomes a
    ``$$`` block, the rest inline ``$`` math.
    """
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for math in list(tree.iter("math")):
        if not isinstance(math, HtmlElement):
            continue
        source = _math_source(math)
        if source:
            fence = "$$" if math.get("display") == "block" else "$"
            math.tail = f"{fence}{source}{fence}{math.tail or ''}"
        math.drop_tree()


def _math_source(math: "HtmlElement") -> str:
    """The formula as TeX where the page carries it, else as the text it renders.

    LaTeXML writes the TeX into ``alttext``. KaTeX writes none and keeps
    it in a TeX annotation, while the MathML's own text reads ``x2`` for
    ``x^2`` — so the annotation is read next, and the text last, with
    every annotation left out of it.
    """
    alttext = (math.get("alttext") or "").strip()
    if alttext:
        return alttext
    tex = "".join(_xpath_text(math, _TEX_ANNOTATION_TEXT)).strip()
    if tex:
        return tex
    return "".join(_xpath_text(math, _RENDERED_MATH_TEXT)).strip()


def _xpath_text(element: "HtmlElement", path: str) -> list[str]:
    result = element.xpath(path)
    return [str(text) for text in result] if isinstance(result, list) else []


def _classes(element: "HtmlElement") -> list[str]:
    return _class_attribute(element).split()


def _class_attribute(element: "HtmlElement") -> str:
    return element.get("class") or ""


@dataclass(frozen=True, slots=True)
class _Page:
    """A successfully fetched page: decoded text plus what the wire said."""

    html: str
    url: str  # where the body came from: what the page's relative URLs resolve against
    body: bytes = b""
    content_type: str = ""


def fetch_article(transport: Transport, extract: HtmlExtract, url: str) -> Outcome:
    """Fetch and extract one page as an article, wayback fallback on failure.

    Args:
        transport: The HTTP seam.
        extract: html -> markdown; injected so callers are hermetic.
        url: The page URL.

    Returns:
        What the fetch found: Content with body and media — the body the
        page's declared markdown source when it has a faithful one — a
        Redetected for a body that is not web content, a thin-extraction
        Unusable, or the classified fetch failure with the wayback
        rescue's fate noted on its evidence.
    """
    page = _fetch_page(transport, url)
    if isinstance(page, _Page):
        redetection = _body_redetection(page)
        if redetection is not None:
            return redetection
        enclosure = audio_enclosure(page.html, url)
        if enclosure is not None and enclosure.declared:
            return Redetected(kind=Kind.PODCAST)
        extracted = _extracted(
            extract,
            page.html,
            base_url=page.url,
            allow_media=True,
            alternate=_markdown_alternate(transport, page),
        )
        if extracted is not None:
            return extracted
        if enclosure is not None:
            # A player and nothing worth extracting: the audio is what
            # the page is. Ordering is the whole rule — an article's
            # read-aloud widget was reached above, by its own body.
            return Redetected(kind=Kind.PODCAST)
        return Unusable(evidence=THIN_EXTRACTION_REASON)
    return _wayback_fallback(transport, extract, url, page)


def _body_redetection(page: _Page) -> Redetected | None:
    """The correction a fetched body calls for, or None for real web content.

    Documents are asked first, and the order is the rule: an extractable
    body — including the signature-less ones only the declared type can
    name — is worth reading, and reading it beats filing it as a picture.
    """
    fmt = _document_format(page)
    if fmt is not None:
        # Detection said web but the body is a document — a HEAD
        # lied or was inconclusive. Re-route, never thin-unusable.
        return Redetected(kind=Kind.FILE, format=fmt)
    if sniff_media_ext(page.body, signatures_only=True) is not None:
        # The body is a picture, not a page: a bare media URL, or a
        # short-link that resolved to one. Extraction must never see it —
        # a megabyte of high-entropy binary decoded with replacement
        # characters clears the substantial bar and is stored as an
        # article of garbage. Signature-backed answers only: the bare
        # MPEG sync-frame range covers the UTF-16 BOM a real page may
        # open with, and filing that page as an mp3 is worse than
        # leaving a signature-less audio URL parked thin.
        return Redetected(kind=Kind.WEB, job=Job.MEDIA)
    return None


def _fetch_page(transport: Transport, url: str) -> _Page | Classification:
    outcome = fetch_classified(transport, url)
    if isinstance(outcome, FetchFailure):
        return outcome.classification
    return _Page(
        html=outcome.text(),
        url=outcome.url or url,
        body=outcome.body,
        content_type=outcome.content_type,
    )


def _extracted(
    extract: HtmlExtract,
    html: str,
    *,
    base_url: str,
    allow_media: bool,
    alternate: str | None = None,
) -> Content | None:
    """The page as Content when its body is substantial, else None."""
    body = _preferred_body(extract(html), alternate)
    if body is None or len(body) < MIN_SUBSTANTIAL_CHARS:
        return None
    media = [image] if allow_media and (image := _og_image(html, base_url)) else []
    meta = {**_title_meta(html), **_description_meta(html)}
    return Content(meta=meta, body=body, media=media)


def _preferred_body(extracted: str | None, alternate: str | None) -> str | None:
    """The declared markdown when it carries at least what extraction found.

    The alternate is the source the page was rendered from, so a faithful
    one holds every word extraction kept plus the tables and code it
    dropped: across 13 docs sites that declare one, it ran 1.05 to 2.9
    times the extraction's length, and 27 times on a page whose HTML
    extracted thin. One shorter than the extraction is missing something
    the page has — a stub, an error body served with a 200 — and where the
    two come close the page lost nothing to extraction, so keeping the
    extraction costs nothing.

    Length alone is not enough, because a source can be longer and hold
    less: Pinecone's learn pages declare one that writes each embedded
    notebook as the CMS's own JSON record, a fenced blob of escaped cells,
    where the page renders its code and output — one post's source ran
    1.58 times the extraction's length with two fewer fenced blocks. So the
    source must also hold at least the extraction's fenced blocks and table
    rows, the structure it exists to keep. Over 72 docs pages that declare
    a source, that turned only Pinecone's three notebook posts back to
    their extraction.
    """
    if alternate is not None and _holds_at_least(alternate, extracted or ""):
        return alternate
    return extracted


def _holds_at_least(source: str, extraction: str) -> bool:
    return all(
        measure(source) >= measure(extraction) for measure in (len, _fence_lines, _table_rows)
    )


def _fence_lines(markdown: str) -> int:
    return len(_FENCE_LINE_RE.findall(markdown))


def _table_rows(markdown: str) -> int:
    return len(_TABLE_ROW_RE.findall(markdown))


def _markdown_alternate(transport: Transport, page: _Page) -> str | None:
    """The markdown source the page declares, fetched where it says, or None.

    Every failure is None and the page's own extraction stands: the
    declaration is an offer, never a dependency.
    """
    url = _markdown_alternate_url(page.html, page.url)
    if url is None:
        return None
    try:
        outcome = fetch_classified(transport, url)
    except ValueError:
        # The transport refusing a host no DNS name can carry: the page's
        # own mistake, and the page is still here to extract.
        return None
    if isinstance(outcome, FetchFailure):
        return None
    text = _markdown_text(outcome)
    return None if text is None else text.strip()


def _markdown_text(response: HttpResponse) -> str | None:
    """A 2xx answer as the markdown asked for, or None when it cannot be that.

    Only a markdown or plain-text answer can be: a JSON error body served
    with a 200 (GitHub Docs' source is an API path) or a feed would
    otherwise win on length alone. The bytes must agree with the label, as
    they must on a page: HTML, XML or JSON under a text label — a soft 404,
    a login wall, an API error — is not markdown, a body a signature names
    is a file whatever type it claims, and one that is not UTF-8 text is no
    markdown at all.
    """
    if response.content_type not in _MARKDOWN_ANSWER_TYPES:
        return None
    if _is_another_document(response.body) or sniff_format(response.body) is not None:
        return None
    if sniff_media_ext(response.body, signatures_only=True) is not None:
        return None
    try:
        return response.body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_another_document(body: bytes) -> bool:
    """Whether a text body is really HTML, XML, a feed or JSON — not markdown.

    ``sniff_document`` names HTML and XML by their openings, but it calls
    anything opening ``[`` or ``{`` JSON, and markdown opens that way too:
    a badge line, an MDX comment. So JSON counts only when the body parses
    as JSON, and a feed is also known by its own root element, which it may
    open with and no prolog.
    """
    shape = sniff_document(body)
    if shape == "JSON":
        try:
            json.loads(body)
        except ValueError:
            return False
        return True
    lead = body.removeprefix(b"\xef\xbb\xbf").lstrip()[:5].lower()
    return shape is not None or lead.startswith(_FEED_LEADS)


def _markdown_alternate_url(html: str, base_url: str) -> str | None:
    for tag in _LINK_TAG_RE.findall(html):
        attributes = _tag_attributes(tag)
        rel = attributes.get("rel", "").lower().split()
        media_type = attributes.get("type", "").split(";")[0].strip().lower()
        href = attributes.get("href", "").strip()
        if "alternate" in rel and media_type == _MARKDOWN_TYPE and href:
            url = urllib.parse.urljoin(base_url, href)
            if url.startswith(("http://", "https://")):
                return url
    return None


def _tag_attributes(tag: str) -> dict[str, str]:
    return {
        name.lower(): html_lib.unescape(double or single or bare)
        for name, double, single, bare in _ATTRIBUTE_RE.findall(tag)
    }


def _wayback_fallback(
    transport: Transport, extract: HtmlExtract, url: str, failure: Classification
) -> Outcome:
    """Try the wayback machine; on any rescue failure, the direct truth stands."""
    snapshot_url, note = _wayback_snapshot(transport, url)
    if snapshot_url is not None:
        page = _fetch_page(transport, snapshot_url)
        if isinstance(page, _Page):
            # Snapshot og:images point at web.archive.org — skip media. The
            # rescue reads the snapshot alone, so a markdown source the
            # page declares is not chased into the archive either.
            rescued = _extracted(extract, page.html, base_url=snapshot_url, allow_media=False)
            if rescued is not None:
                meta = dict(rescued.meta)
                meta["via"] = "wayback"
                meta["snapshot"] = snapshot_url
                return Content(meta=meta, body=rescued.body)
            note = "wayback snapshot extraction was thin"
        else:
            note = f"wayback fetch failed: {page.reason}"
    if note is not None:
        # The rescue's fate is appended context on the direct truth —
        # the classification's own finding and evidence still decide.
        failure = replace(failure, reason=f"{failure.reason}; {note}")
    return failure.to_outcome()


def _wayback_snapshot(transport: Transport, url: str) -> tuple[str | None, str | None]:
    """The closest snapshot URL, or (None, why the lookup yielded nothing)."""
    lookup = _WAYBACK_AVAILABLE + urllib.parse.quote(url, safe="")
    outcome = fetch_classified(transport, lookup)
    if isinstance(outcome, FetchFailure):
        # A failed rescue is only a note on the direct failure — the
        # wire fact, never the classifier's status framing.
        return None, f"wayback lookup failed: {outcome.detail}"
    try:
        snapshots = json.loads(outcome.text())
    except json.JSONDecodeError:
        return None, "wayback lookup returned unparseable JSON"
    if not isinstance(snapshots, dict):
        return None, "wayback lookup returned an unexpected shape"
    archived = snapshots.get("archived_snapshots")
    closest = archived.get("closest") if isinstance(archived, dict) else None
    if not isinstance(closest, dict) or not closest.get("available") or not closest.get("url"):
        return None, "no wayback snapshot"
    return closest["url"], None


def _document_format(page: _Page) -> Format | None:
    """The extractable Format of a fetched body, or None for real web content.

    Magic bytes decide first (authoritative — servers lie in both
    directions); the declared content type is the fallback that catches
    signature-less formats (CSV). No filename-extension guessing here: a
    web URL's path tail is routing, not identity.
    """
    magic = sniff_format(page.body)
    if magic is not None:
        return magic
    return CONTENT_TYPE_FORMATS.get(page.content_type)


def _og_image(html: str, base_url: str) -> str | None:
    """The page's og:image as an absolute http(s) URL, or None.

    The media stage fetches what it is handed verbatim, so a media URL must
    be fetchable by construction: relative and protocol-relative values
    resolve against the page they were found on, entities unescape, and
    anything that is not http(s) afterwards (``data:``, ``javascript:``, a
    template placeholder) is not a media URL at all.
    """
    for pattern in _OG_IMAGE_RES:
        match = pattern.search(html)
        if match is None:
            continue
        candidate = urllib.parse.urljoin(base_url, html_lib.unescape(match.group(1)).strip())
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def _title_meta(html: str) -> dict[str, str | int | None]:
    match = _TITLE_RE.search(html)
    if not match:
        return {}
    title = " ".join(html_lib.unescape(match.group(1)).split())[:_MAX_TITLE_CHARS]
    return {"title": title} if title else {}


def _description_meta(html: str) -> dict[str, str | int | None]:
    for pattern in _DESCRIPTION_RES:
        match = pattern.search(html)
        if match is None:
            continue
        description = " ".join(html_lib.unescape(match.group(1)).split())[:_MAX_DESCRIPTION_CHARS]
        if description:
            return {"description": description}
    return {}
