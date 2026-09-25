"""Tests for drivers/paper.py: arxiv API + full text, the article route."""

import re

from hypothesis import assume, given
from hypothesis import strategies as st

from dex_engine.drivers.article import trafilatura_extract
from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.types import Content, Kind, Missing, Refused
from dex_engine.pipeline.urls import work_hash
from tests.drivers.conftest import FakeTransport, body_of, content_of, fixture_text, make_unit

ABS_URL = "https://arxiv.org/abs/2408.12345"
API_URL = "https://export.arxiv.org/api/query?id_list=2408.12345"
HTML_URL = "https://arxiv.org/html/2408.12345"
AR5IV_URL = "https://ar5iv.labs.arxiv.org/html/2408.12345"
FEED = fixture_text("paper", "arxiv-feed.xml")
EMPTY_FEED = fixture_text("paper", "arxiv-empty-feed.xml")
# arXiv's own HTML renderings (LaTeXML), trimmed to the parts that lost
# content in the field: 2609.15779 lost every inline value and its ablation
# cells, 2609.30094 its equations and its table-only appendices.
EVO_ONTOLOGY = fixture_text("paper", "arxiv-2609.15779.html")
PRIV_DRIFT = fixture_text("paper", "arxiv-2609.30094.html")


def xml_response(text: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="application/atom+xml", body=text.encode())


def html_page(text: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="text/html", body=text.encode())


def long_extract(_html: str) -> str:
    return "Section 1. Introduction. " * 200


def thin_extract(_html: str) -> str:
    return "nav chrome"


def driver_for(responses: dict, extract=long_extract) -> PaperDriver:
    return PaperDriver(transport=FakeTransport(responses), extract=extract)


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.PAPER
        assert driver.sleep == 3.0

    def test_matches_arxiv_openreview_and_hf_papers(self):
        driver = driver_for({})
        assert driver.matches("https://arxiv.org/abs/2408.12345")
        assert driver.matches("https://openreview.net/forum?id=abc")
        assert driver.matches("https://huggingface.co/papers/2408.12345")
        assert not driver.matches("https://huggingface.co/models/acme/thing")
        assert not driver.matches("https://example.test/paper")

    def test_canonical_collapses_abs_pdf_html_and_versions_to_one_key(self):
        driver = driver_for({})
        for url in (
            "https://arxiv.org/abs/2408.12345",
            "https://arxiv.org/pdf/2408.12345",
            "https://arxiv.org/pdf/2408.12345.pdf",
            "https://arxiv.org/pdf/2408.12345v2",
            "https://arxiv.org/html/2408.12345v1",
            "https://arxiv.org/abs/2408.12345v5/",
            "http://www.arxiv.org/abs/2408.12345",
            "https://arxiv.org/abs/2408.12345?context=cs.CL",
            "https://arxiv.org/abs/2408.12345#S3",
        ):
            assert driver.canonical(url) == ABS_URL

    def test_canonical_is_idempotent_across_the_rewrite(self):
        driver = driver_for({})
        once = driver.canonical("https://arxiv.org/pdf/2408.12345v2.pdf")
        assert driver.canonical(once) == once

    def test_canonical_collapses_old_style_spellings_to_one_key(self):
        # Pre-2007 ids spell <archive>[.<subclass>]/<YYMMNNN>. The subject
        # class is dropped with the version: the export API resolves only
        # the bare <archive>/<number> form, so the spellings are one fetch.
        driver = driver_for({})
        for url in (
            "https://arxiv.org/abs/math/0309285",
            "https://arxiv.org/abs/math/0309285v2",
            "https://arxiv.org/pdf/math/0309285.pdf",
            "https://arxiv.org/html/math/0309285v1",
            "https://arxiv.org/abs/math.NA/0309285",
            "https://arxiv.org/abs/math.NA/0309285v2/",
            "http://www.arxiv.org/abs/math/0309285?context=cs.DS",
        ):
            assert driver.canonical(url) == "https://arxiv.org/abs/math/0309285"

    def test_old_style_canonical_is_idempotent_across_the_rewrite(self):
        driver = driver_for({})
        once = driver.canonical("https://arxiv.org/pdf/math.NA/0309285v2.pdf")
        assert driver.canonical(once) == once

    def test_non_arxiv_paper_keys_keep_the_generic_canonical_form(self):
        driver = driver_for({})
        assert driver.canonical("https://openreview.net/forum?id=abc") == (
            "https://openreview.net/forum?id=abc"
        )
        assert driver.canonical("https://huggingface.co/papers/2408.12345") == (
            "https://huggingface.co/papers/2408.12345"
        )
        # An arxiv URL that addresses no single paper is not rewritten either.
        assert driver.canonical("https://arxiv.org/list/cs.AI/recent") == (
            "https://arxiv.org/list/cs.AI/recent"
        )
        assert driver.canonical("https://arxiv.org/archive/math") == (
            "https://arxiv.org/archive/math"
        )
        # Six digits is not an old-style id — YYMMNNN is seven.
        assert driver.canonical("https://arxiv.org/abs/math/030928") == (
            "https://arxiv.org/abs/math/030928"
        )
        # An arxiv-shaped path on another host names no arxiv paper.
        assert driver.canonical("https://example.test/abs/math/0309285") == (
            "https://example.test/abs/math/0309285"
        )


# Modern arxiv ids are YYMM.NNNNN, optionally versioned; the strategy
# generates the number space the driver has to key on.
_arxiv_ids = st.builds(
    "{}.{}".format,
    st.integers(min_value=1, max_value=9999).map(lambda n: f"{n:04d}"),
    st.integers(min_value=1, max_value=99999).map(lambda n: f"{n:05d}"),
)
_versions = st.sampled_from(["", "v1", "v2", "v11"])
# Old-style (pre-2007) ids are <archive>[.<subclass>]/<YYMMNNN>; the
# archive and subclass spellings come from arxiv's own taxonomy, hyphens
# and both letter cases included.
_old_archives = st.sampled_from(["math", "cs", "hep-th", "astro-ph", "cond-mat", "q-bio"])
_old_subclasses = st.sampled_from(["", ".GT", ".AI", ".CO", ".str-el", ".dis-nn"])
_old_numbers = st.integers(min_value=0, max_value=9999999).map(lambda n: f"{n:07d}")


def _paper_shapes(arxiv_id: str, version: str) -> list[str]:
    return [
        f"https://arxiv.org/abs/{arxiv_id}{version}",
        f"https://www.arxiv.org/abs/{arxiv_id}{version}",
        f"https://arxiv.org/abs/{arxiv_id}{version}/",
        f"https://arxiv.org/abs/{arxiv_id}{version}?context=cs.CL",
        f"https://arxiv.org/pdf/{arxiv_id}{version}",
        f"https://arxiv.org/pdf/{arxiv_id}{version}.pdf",
        f"https://arxiv.org/html/{arxiv_id}{version}",
    ]


class TestIdentityProperties:
    @given(_arxiv_ids, _versions)
    def test_every_spelling_of_a_paper_is_one_work_unit(self, arxiv_id, version):
        # Five spellings of one paper were five ledger entries, five
        # enrichment files and five copies of one abstract in the digest.
        driver = driver_for({})
        canonicals = {driver.canonical(shape) for shape in _paper_shapes(arxiv_id, version)}
        assert canonicals == {f"https://arxiv.org/abs/{arxiv_id}"}

    @given(_arxiv_ids)
    def test_versions_key_together_because_the_fetch_is_versionless(self, arxiv_id):
        # The API is queried by bare id and returns latest, so a v5-keyed
        # unit would fetch exactly what the unversioned one fetches.
        driver = driver_for({})
        keys = {work_hash(driver.canonical(url)) for url in _paper_shapes(arxiv_id, "v5")}
        assert keys == {work_hash(f"https://arxiv.org/abs/{arxiv_id}")}

    @given(_arxiv_ids, _arxiv_ids)
    def test_different_papers_stay_different_work_units(self, id_a, id_b):
        assume(id_a != id_b)
        driver = driver_for({})
        hashes_a = {work_hash(driver.canonical(s)) for s in _paper_shapes(id_a, "")}
        hashes_b = {work_hash(driver.canonical(s)) for s in _paper_shapes(id_b, "v2")}
        assert hashes_a.isdisjoint(hashes_b)

    @given(_old_archives, _old_subclasses, _old_numbers, _versions)
    def test_every_spelling_of_an_old_style_paper_is_one_work_unit(
        self, archive, subclass, number, version
    ):
        # The subject class goes the way of the version: the export API
        # resolves only the bare <archive>/<number> form, so math.GT/0309136
        # and math/0309136v2 fetch exactly what math/0309136 fetches.
        driver = driver_for({})
        canonicals = {
            driver.canonical(shape)
            for shape in _paper_shapes(f"{archive}{subclass}/{number}", version)
        }
        assert canonicals == {f"https://arxiv.org/abs/{archive}/{number}"}

    @given(_old_archives, _old_numbers, _old_archives, _old_numbers)
    def test_different_old_style_papers_stay_different_work_units(
        self, archive_a, number_a, archive_b, number_b
    ):
        assume((archive_a, number_a) != (archive_b, number_b))
        driver = driver_for({})
        shapes_a = _paper_shapes(f"{archive_a}/{number_a}", "")
        shapes_b = _paper_shapes(f"{archive_b}/{number_b}", "v2")
        hashes_a = {work_hash(driver.canonical(s)) for s in shapes_a}
        hashes_b = {work_hash(driver.canonical(s)) for s in shapes_b}
        assert hashes_a.isdisjoint(hashes_b)

    @given(_old_archives, _old_numbers, _arxiv_ids)
    def test_old_and_new_style_papers_stay_different_work_units(self, archive, number, new_id):
        driver = driver_for({})
        old_hashes = {
            work_hash(driver.canonical(s)) for s in _paper_shapes(f"{archive}/{number}", "")
        }
        new_hashes = {work_hash(driver.canonical(s)) for s in _paper_shapes(new_id, "")}
        assert old_hashes.isdisjoint(new_hashes)


class TestArxiv:
    def test_the_canonical_key_is_what_fetch_reads(self):
        # Identity and content must not diverge: the form canonical emits
        # has to route through the arxiv API, never the article route — an
        # article-route GET of the abs URL would hit FakeTransport's loud
        # unknown-URL failure.
        driver = PaperDriver(
            transport=FakeTransport(
                {API_URL: xml_response(FEED), HTML_URL: html_page("<html>paper</html>")}
            ),
            extract=long_extract,
        )
        canonical = driver.canonical("https://arxiv.org/pdf/2408.12345v2.pdf")
        result = content_of(driver.fetch(make_unit(canonical, Kind.PAPER)))
        assert result.meta["arxiv_id"] == "2408.12345"

    def test_an_old_style_id_fetches_through_the_api_not_the_article_route(self):
        # An old-style abs page read as an article was the bug: the id has
        # to reach the export API, in the bare form the API resolves.
        api_url = "https://export.arxiv.org/api/query?id_list=math/0309285"
        html_url = "https://arxiv.org/html/math/0309285"
        driver = PaperDriver(
            transport=FakeTransport(
                {api_url: xml_response(FEED), html_url: html_page("<html>paper</html>")}
            ),
            extract=long_extract,
        )
        canonical = driver.canonical("https://arxiv.org/abs/math.NA/0309285v2")
        result = content_of(driver.fetch(make_unit(canonical, Kind.PAPER)))
        assert result.meta["arxiv_id"] == "math/0309285"

    def test_abstract_and_full_text_sections(self):
        responses = {API_URL: xml_response(FEED), HTML_URL: html_page("<html>paper</html>")}
        result = content_of(driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER)))
        body = body_of(result)
        assert body.startswith("## Abstract")
        assert "the work queue rather than a receipt log" in body
        assert "## Full text" in body
        assert result.meta.get("note") is None

    def test_meta_title_collapsed_published_date_and_id(self):
        responses = {API_URL: xml_response(FEED), HTML_URL: html_page("x")}
        meta = content_of(driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))).meta
        assert meta["title"] == (
            "Ledger-Driven Ingestion: Honest Failure Classification for Long-Lived Corpora"
        )
        assert meta["published"] == "2026-07-15"
        assert meta["arxiv_id"] == "2408.12345"

    def test_ar5iv_fallback_when_arxiv_html_is_missing(self):
        responses = {
            API_URL: xml_response(FEED),
            HTML_URL: html_page("nope", status=404),
            AR5IV_URL: html_page("<html>rendered</html>"),
        }
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert isinstance(result, Content)
        assert "## Full text" in body_of(result)

    def test_the_full_text_runs_through_the_real_extractor(self):
        # No extractor injected: the driver's own wiring is under test, and
        # the page preparation reaches the arXiv rendering through it.
        responses = {API_URL: xml_response(FEED), HTML_URL: html_page(PRIV_DRIFT)}
        driver = PaperDriver(transport=FakeTransport(responses))
        body = body_of(driver.fetch(make_unit(ABS_URL, Kind.PAPER)))
        full_text = body[body.index("## Full text") :]
        assert "We generate $N=1000$ controlled dialogues" in full_text
        assert "## Appendix A Dataset Characterization" in full_text
        assert "| 0 | 197 | 44.29 | 43.00 | 59.05 | 57.33 |" in full_text

    def test_thin_full_text_degrades_to_abstract_only_with_note(self):
        responses = {
            API_URL: xml_response(FEED),
            HTML_URL: html_page("<html>chrome</html>"),
            AR5IV_URL: html_page("<html>chrome</html>"),
        }
        result = content_of(
            driver_for(responses, extract=thin_extract).fetch(make_unit(ABS_URL, Kind.PAPER))
        )
        assert result.meta["note"] == "abstract only"
        assert "## Full text" not in body_of(result)

    def test_unknown_id_is_dead(self):
        responses = {API_URL: xml_response(EMPTY_FEED)}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert isinstance(result, Missing)
        assert "no such id" in result.evidence

    def test_api_failures_are_classified(self):
        responses = {API_URL: xml_response("busy", status=503)}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert isinstance(result, Refused)

    def test_unparseable_feed_is_blocked(self):
        responses = {API_URL: xml_response("<feed><unclosed")}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert isinstance(result, Refused)
        assert "unparseable XML" in result.evidence


class TestFullTextFidelity:
    """What an arXiv rendering keeps through extraction.

    LaTeXML writes every formula as MathML, every numbered equation as a
    table, and a table that sits inside running text as spans — three
    shapes the extractor deleted or emptied.
    """

    def test_inline_math_keeps_the_values_the_prose_reports(self):
        body = trafilatura_extract(EVO_ONTOLOGY) or ""
        assert "with an average gain of $+17.8$ points over Baseline" in body
        assert "removing the gate causes the largest drop ($-11.2$ Traj-Wise)" in body
        assert "Removing the attribution step drops by $-6.3$" in body
        assert (
            "no pair exceeds $0.62$ overlap, and the two Claude backbones share less with"
            " each other ($0.55$) than the two GPT backbones do ($0.61$)"
        ) in body
        assert "Tool-level edits account for $57\\%$ of the cumulative gain" in body

    def test_an_ablation_tables_cells_keep_their_values(self):
        body = trafilatura_extract(EVO_ONTOLOGY) or ""
        for row in (
            "| w/o Gate | $78.3$ | $-11.2$ |",
            "| w/o Attribution | $83.2$ | $-6.3$ |",
            "| w/o Diagnose | $84.7$ | $-4.8$ |",
            "| w/o Patch (free-form) | $87.8$ | $-1.7$ |",
        ):
            assert row in body

    def test_a_stacked_header_cell_stays_inside_its_cell(self):
        # "Msg-Wise" over "(%, ↑)" is a tabular nested in a table cell, and a
        # table inside a cell has no markdown form: it stays that cell's text.
        body = trafilatura_extract(EVO_ONTOLOGY) or ""
        assert (
            "| Method | Backbone | Msg-Wise (%, $\\uparrow$) | Traj-Wise (%, $\\uparrow$)"
            " | Overall (%, $\\uparrow$) |"
        ) in body

    def test_an_equation_table_is_display_math_not_empty_cells(self):
        body = trafilatura_extract(PRIV_DRIFT) or ""
        assert (
            "$$d\\sim\\begin{cases}0&\\text{with }p=0.2,\\\\ \\mathrm{Uniform}\\{2,3,4,5,6\\}"
            "&\\text{with }p=0.8.\\end{cases}$$ (1)"
        ) in body
        assert not re.search(r"^\|(\s*\|)+\s*(\(\d+\)\s*\|)?\s*$", body, re.MULTILINE)

    def test_an_equation_row_without_math_keeps_its_text(self):
        # A row can carry words between the lines of an aligned equation.
        table = (
            '<table class="ltx_equation ltx_eqn_table"><tbody>'
            '<tr class="ltx_equation ltx_eqn_row"><td class="ltx_eqn_cell"></td>'
            '<td class="ltx_eqn_cell"><math alttext="L(d)\\leq\\delta" display="block">'
            "<mi>L</mi></math></td>"
            '<td class="ltx_eqn_cell ltx_eqn_eqno"><span class="ltx_tag">(9)</span></td></tr>'
            '<tr class="ltx_eqn_row"><td class="ltx_eqn_cell">for every drift length observed,</td>'
            "</tr></tbody></table>"
        )
        body = trafilatura_extract(in_paper(table)) or ""
        assert "$$L(d)\\leq\\delta$$ (9)\n\nfor every drift length observed,\n" in body

    def test_an_aligned_equations_cells_join_into_one_display_block(self):
        # An aligned equation puts each side in its own cell; the row is
        # still one formula, and its number follows it.
        body = trafilatura_extract(EVO_ONTOLOGY) or ""
        assert (
            "$$\\displaystyle\\mathcal{C}^{+} \\displaystyle=\\left\\{c\\in\\mathcal{C}\\,"
            "\\middle|\\,\\mathrm{verify}\\!\\left(\\mathrm{probe}(c,\\mathcal{D})\\right)=1"
            "\\right\\},$$ (1)"
        ) in body

    def test_table_only_appendices_after_the_references_survive(self):
        # Their tables went with the figures that float them, the bare
        # headings left behind trailed the page, and trailing headings are
        # stripped: both appendices vanished whole.
        body = trafilatura_extract(PRIV_DRIFT) or ""
        references = body.index("## References")
        appendix_a = body.index("## Appendix A Dataset Characterization")
        appendix_b = body.index("## Appendix B Additional Effect Sizes")
        assert references < appendix_a < appendix_b
        assert "| 0 | 197 | 44.29 | 43.00 | 59.05 | 57.33 |" in body[appendix_a:appendix_b]
        assert "| DeepSeek-R1 | Secret type | 0.5871 | Large |" in body[appendix_b:]

    def test_a_span_tabular_in_the_body_is_a_table(self):
        body = trafilatura_extract(PRIV_DRIFT) or ""
        assert "| Total dialogues | 1,000 |" in body
        assert "| Drift lengths | $0,2,3,4,5,6$ |" in body

    def test_a_tabulars_rows_may_sit_straight_under_it(self):
        # Head and body wrappers are LaTeXML's option, not a rule.
        tabular = latexml_tabular(("Msg-Wise", "Traj-Wise"), ("(%, up)", "(%, down)"))
        body = trafilatura_extract(in_paper(f'<p class="ltx_p">{tabular}</p>')) or ""
        assert "| Msg-Wise | Traj-Wise |" in body
        assert "| (%, up) | (%, down) |" in body

    def test_a_tabular_inside_a_header_cell_stays_that_cells_text(self):
        stacked = latexml_tabular(("Msg-Wise",), ("(%, up)",))
        table = (
            '<table class="ltx_tabular"><tr class="ltx_tr">'
            f'<th class="ltx_td">{stacked}</th><th class="ltx_td">Backbone</th></tr>'
            '<tr class="ltx_tr"><td class="ltx_td">74.0</td><td class="ltx_td">GPT-5.5</td></tr>'
            "</table>"
        )
        body = trafilatura_extract(in_paper(table)) or ""
        assert "| Msg-Wise (%, up) | Backbone |" in body


def latexml_tabular(*rows: tuple[str, ...]) -> str:
    """A span-typeset LaTeXML tabular, its rows straight under it."""
    spans = "".join(
        '<span class="ltx_tr">'
        + "".join(f'<span class="ltx_td ltx_align_center">{cell}</span>' for cell in row)
        + "</span>\n"
        for row in rows
    )
    return f'<span class="ltx_tabular ltx_align_middle">\n{spans}</span>'


def in_paper(markup: str) -> str:
    """The 2609.30094 rendering with ``markup`` as a paragraph of its section 3.2."""
    return PRIV_DRIFT.replace(
        '<figure id="S3.T1"', f'<div class="ltx_para">{markup}</div>\n<figure id="S3.T1"', 1
    )


class TestArticleRoute:
    def test_non_arxiv_papers_read_as_articles(self):
        # The shared article seam does the fetching: the page URL itself is
        # GET, extracted, and lands done — the ledger kind stays paper.
        url = "https://openreview.net/forum?id=abc"
        transport = FakeTransport({url: html_page("<html>rev</html>")})
        driver = PaperDriver(transport=transport, extract=long_extract)
        result = driver.fetch(make_unit(url, Kind.PAPER))
        assert ("GET", url) in transport.calls
        assert isinstance(result, Content)
