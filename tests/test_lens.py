"""Tests for lens.py: whether ``lens.md`` states what the instance reads for.

The placeholder lines are read off the seed, so these tests run against the
tree's own ``instance/lens.md`` — the file the wheel bundles — and one
invented seed, which proves they are derived rather than remembered.
"""

from pathlib import Path

import pytest

from dex_engine.lens import LensReading, read_lens
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

PLACEHOLDERS_LEFT = "until the placeholders are replaced (or the file deleted)"
GENERAL = "this dex reads as general knowledge"


def reading(instance: Instance, lens: str | bytes | None, template: Path = TEMPLATE) -> LensReading:
    if isinstance(lens, bytes):
        instance.lens_path.write_bytes(lens)
    elif lens is not None:
        instance.lens_path.write_text(lens, encoding="utf-8")
    return read_lens(instance, template)


def placeholders_left(named: str) -> LensReading:
    """The reading of a lens.md still holding the placeholder lines ``named``."""
    finding = f"`lens.md` still holds the seed's placeholder text ({named})"
    return LensReading(
        text=None, finding=finding, note=f"{finding}: {PLACEHOLDERS_LEFT}, {GENERAL}"
    )


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

    def test_the_seed_itself_reads_as_general_knowledge_with_a_note(self, instance):
        assert reading(instance, SEED) == placeholders_left(
            "`<what this instance takes from whatever is shared into it>`, "
            "`<what to look at hardest>`, `<what to ignore, even when it is the subject>`"
        )

    def test_the_title_line_is_not_a_placeholder(self, instance):
        # `# <instance name>` carries a placeholder but is a heading, not a
        # line that is only one: a lens keeping it states a lens all the same.
        lens = "# <instance name>\n\nEverything about espresso.\n"
        assert reading(instance, lens).text == lens.strip()


class TestAStatedLens:
    def test_is_its_text_stripped_with_nothing_to_say(self, instance):
        assert reading(instance, f"\n{FILLED}\n\n") == LensReading(
            text=FILLED.strip(), finding=None, note=None
        )

    def test_a_placeholder_quoted_inside_a_sentence_is_prose(self, instance):
        lens = "Reads for design; ignore the old `<what to look at hardest>` prompt.\n"
        assert reading(instance, lens).text == lens.strip()

    def test_a_lens_that_deleted_the_optional_headings_states_one(self, instance):
        lens = "# dex-design\n\n## Reads for\nHow things look and work.\n"
        assert reading(instance, lens).text == lens.strip()

    def test_a_free_form_lens_with_no_headings_states_one(self, instance):
        lens = "Read everything for how it is designed: a site like this, for its type pairing.\n"
        assert reading(instance, lens).text == lens.strip()


class TestNoLensAtAll:
    """A general knowledge dex: a legitimate way to run an instance, so no note."""

    def test_missing(self, instance):
        assert reading(instance, None) == LensReading(
            text=None, finding="`lens.md` is missing", note=None
        )

    @pytest.mark.parametrize("text", ["", "\n", "  \n\t\n   "])
    def test_empty_or_whitespace_only(self, instance, text):
        assert reading(instance, text) == LensReading(
            text=None, finding="`lens.md` is empty", note=None
        )


class TestANote:
    """A lens.md that is there and still reads as general knowledge."""

    def test_unreadable_bytes(self, instance):
        finding = "`lens.md` is unreadable (UnicodeDecodeError)"
        assert reading(instance, b"\xff\xfe not text") == LensReading(
            text=None,
            finding=finding,
            note=f"{finding}: until it can be read (or the file deleted), {GENERAL}",
        )

    def test_a_directory_in_its_place(self, instance):
        instance.lens_path.mkdir()
        said = reading(instance, None)
        assert said.text is None
        assert said.finding == "`lens.md` is unreadable (IsADirectoryError)"
        assert said.note is not None
        assert said.note.startswith(f"{said.finding}: until it can be read")

    def test_a_partly_filled_lens_names_the_placeholder_left(self, instance):
        partly = FILLED.replace("The subject matter, entirely.", "<what to look at hardest>")
        assert reading(instance, partly) == placeholders_left("`<what to look at hardest>`")

    def test_a_placeholder_line_is_found_whatever_surrounds_it(self, instance):
        # Verbatim means the line's text: indentation and trailing blanks
        # an editor left do not make a placeholder a statement.
        said = reading(instance, FILLED + "\n   <what to ignore, even when it is the subject>  \n")
        assert said == placeholders_left("`<what to ignore, even when it is the subject>`")


class TestDerivedFromTheSeed:
    def test_the_placeholders_are_whatever_the_seed_holds(self, instance, tmp_path):
        seed = tmp_path / "template"
        seed.mkdir()
        (seed / "lens.md").write_text("# <name>\n\n<a prompt of its own>\nprose, not a prompt\n")
        assert reading(instance, FILLED, seed).text == FILLED.strip()
        assert reading(instance, "<what to look at hardest>\n", seed).note is None
        assert reading(instance, "<a prompt of its own>\n", seed) == placeholders_left(
            "`<a prompt of its own>`"
        )

    def test_a_missing_lens_never_reads_the_seed(self, instance, tmp_path):
        # Nothing to compare, so the reading stands on the instance alone.
        said = reading(instance, None, tmp_path / "no-template")
        assert said.finding == "`lens.md` is missing"

    def test_an_empty_lens_never_reads_the_seed(self, instance, tmp_path):
        said = reading(instance, "\n", tmp_path / "no-template")
        assert said.finding == "`lens.md` is empty"
