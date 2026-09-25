"""Tests for lens.py: whether ``lens.md`` states what the instance reads for.

The placeholder lines are read off the seed, so these tests run against the
tree's own ``instance/lens.md`` — the file the wheel bundles — and one
invented seed, which proves they are derived rather than remembered.
"""

from pathlib import Path

import pytest

from dex_engine.lens import lens_finding
from dex_engine.pipeline.types import Instance

TEMPLATE = Path(__file__).resolve().parent.parent / "instance"
SEED = (TEMPLATE / "lens.md").read_text(encoding="utf-8")

FILLED = """\
# dex-design

## Reads for
How things are made to look and work.

## Emphasise
Layout, typography, colour and interaction.

## Set aside
The subject matter, entirely.
"""


def finding(instance: Instance, lens: str | bytes | None, template: Path = TEMPLATE) -> str | None:
    if isinstance(lens, bytes):
        instance.lens_path.write_bytes(lens)
    elif lens is not None:
        instance.lens_path.write_text(lens, encoding="utf-8")
    return lens_finding(instance, template)


class TestTheSeed:
    def test_every_prompt_under_a_heading_is_a_placeholder_line(self):
        # The seed is the one source of the placeholder text; this pins what
        # it says, so an edit to it is a decision rather than an accident.
        assert SEED == (
            "# <instance name>\n\n"
            "## Reads for\n<what this instance takes from whatever is shared into it>\n\n"
            "## Emphasise\n<what to look at hardest>\n\n"
            "## Set aside\n<what to ignore, even when it is the subject>\n"
        )

    def test_the_seed_itself_is_unfilled(self, instance):
        said = finding(instance, SEED)
        assert said == (
            "`lens.md` still holds the seed's placeholder text: "
            "`<what this instance takes from whatever is shared into it>`, "
            "`<what to look at hardest>`, `<what to ignore, even when it is the subject>`"
        )

    def test_the_title_line_is_not_a_placeholder(self, instance):
        # `# <instance name>` carries a placeholder but is a heading, not a
        # line that is only one: a lens keeping it states a lens all the same.
        assert finding(instance, "# <instance name>\n\nEverything about espresso.\n") is None


class TestFinding:
    def test_a_filled_lens_states_one(self, instance):
        assert finding(instance, FILLED) is None

    def test_missing(self, instance):
        assert finding(instance, None) == "`lens.md` is missing"

    @pytest.mark.parametrize("text", ["", "\n", "  \n\t\n   "])
    def test_empty_or_whitespace_only(self, instance, text):
        assert finding(instance, text) == "`lens.md` is empty"

    def test_unreadable_bytes(self, instance):
        assert finding(instance, b"\xff\xfe not text") == (
            "`lens.md` is unreadable (UnicodeDecodeError)"
        )

    def test_a_directory_in_its_place(self, instance):
        instance.lens_path.mkdir()
        assert finding(instance, None) == "`lens.md` is unreadable (IsADirectoryError)"

    def test_a_partly_filled_lens_names_the_placeholder_left(self, instance):
        partly = FILLED.replace("The subject matter, entirely.", "<what to look at hardest>")
        assert finding(instance, partly) == (
            "`lens.md` still holds the seed's placeholder text: `<what to look at hardest>`"
        )

    def test_a_placeholder_line_is_found_whatever_surrounds_it(self, instance):
        # Verbatim means the line's text: indentation and trailing blanks
        # an editor left do not make a placeholder a statement.
        said = finding(instance, FILLED + "\n   <what to ignore, even when it is the subject>  \n")
        assert said is not None
        assert "`<what to ignore, even when it is the subject>`" in said

    def test_a_placeholder_quoted_inside_a_sentence_is_prose(self, instance):
        lens = "Reads for design; ignore the old `<what to look at hardest>` prompt.\n"
        assert finding(instance, lens) is None

    def test_a_lens_that_deleted_the_optional_headings_passes(self, instance):
        assert (
            finding(instance, "# dex-design\n\n## Reads for\nHow things look and work.\n") is None
        )

    def test_a_free_form_lens_with_no_headings_passes(self, instance):
        lens = "Read everything for how it is designed: a site like this, for its type pairing.\n"
        assert finding(instance, lens) is None


class TestDerivedFromTheSeed:
    def test_the_placeholders_are_whatever_the_seed_holds(self, instance, tmp_path):
        seed = tmp_path / "template"
        seed.mkdir()
        (seed / "lens.md").write_text("# <name>\n\n<a prompt of its own>\nprose, not a prompt\n")
        assert finding(instance, FILLED, seed) is None
        assert finding(instance, "<what to look at hardest>\n", seed) is None
        assert finding(instance, "<a prompt of its own>\n", seed) == (
            "`lens.md` still holds the seed's placeholder text: `<a prompt of its own>`"
        )

    def test_a_missing_lens_never_reads_the_seed(self, instance, tmp_path):
        # Nothing to compare, so the finding stands on the instance alone.
        assert finding(instance, None, tmp_path / "no-template") == "`lens.md` is missing"
