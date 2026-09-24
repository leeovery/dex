"""Tests for directive 2: README.md rewritten from the engine's template."""

from pathlib import Path

import pytest

from dex_engine import seeds
from dex_engine.directive import MATERIALS_END
from dex_engine.directives import directive_2, discover
from dex_engine.directives.directive_2 import check, materials, unmet

TEMPLATE = Path(__file__).resolve().parents[2] / "instance"
SSH = "git@github.com:someone/dex-cooking.git"
REWRITE = "write it with `bin/dex directive show 2 --materials > README.md`"
NOT_THE_TEMPLATE = (
    f"`README.md` is not the engine's template rendered for this instance: {REWRITE} "
    "and change nothing after"
)


def origin_of(url: str | None):
    return lambda _root: url


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "dex-cooking"
    root.mkdir()
    return root


def rendered(root: Path, url: str | None = SSH) -> str:
    return seeds.instance_readme(TEMPLATE, root, origin=origin_of(url))


def readme(root: Path, text: str) -> None:
    (root / "README.md").write_text(text, encoding="utf-8")


class TestUnmet:
    def test_the_rendered_readme_meets_the_condition(self, root):
        readme(root, rendered(root))
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == []

    def test_any_spelling_of_the_same_repo_renders_the_same_readme(self, root):
        readme(root, rendered(root, SSH))
        https = "https://github.com/someone/dex-cooking"
        assert unmet(root, TEMPLATE, origin=origin_of(https)) == []

    def test_a_local_only_instance_needs_the_readme_without_the_join_section(self, root):
        readme(root, rendered(root, None))
        assert unmet(root, TEMPLATE, origin=origin_of(None)) == []
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == [NOT_THE_TEMPLATE]

    def test_a_missing_readme_is_unmet(self, root):
        missing = f"`README.md` is missing: {REWRITE}"
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == [missing]

    def test_an_unreadable_readme_is_unmet(self, root):
        (root / "README.md").write_bytes(b"# dex-cooking\n\xff\xfe\n")
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == [
            f"`README.md` is unreadable (UnicodeDecodeError): {REWRITE}"
        ]

    @pytest.mark.parametrize(
        "change",
        [
            lambda text: text[: len(text) // 2],
            lambda text: text.replace("Ask Claude to", "Ask Claude when you want to"),
            lambda text: text.rstrip("\n"),
            lambda text: text + "\nAn owner's line.\n",
            lambda text: text.replace("someone/dex-cooking", "someone/other"),
            lambda _text: seeds.readme(TEMPLATE, "dex-cooking"),
        ],
        ids=["partial", "reworded", "no-final-newline", "added-to", "other-repo", "placeholder"],
    )
    def test_anything_but_the_exact_render_is_unmet(self, root, change):
        text = change(rendered(root))
        assert text != rendered(root)
        readme(root, text)
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == [NOT_THE_TEMPLATE]

    def test_the_old_scope_readme_is_unmet(self, root):
        readme(root, "# dex-cooking\n\n## In scope\n\n- weeknight recipes\n")
        assert unmet(root, TEMPLATE, origin=origin_of(SSH)) == [NOT_THE_TEMPLATE]


class TestShipped:
    def directive(self):
        [shipped] = [directive for directive in discover() if directive.number == 2]
        return shipped

    def test_it_ships_as_directive_2_with_its_check_and_materials(self):
        directive = self.directive()
        assert directive.intent == directive_2.INTENT
        assert directive.check is check
        assert directive.materials is materials

    def test_its_instructions_write_the_readme_the_way_its_check_says_to(self):
        instructions = self.directive().instructions
        assert "`bin/dex directive show 2 --materials > README.md`" in instructions
        assert "`bin/dex directive show 2 --materials > README.md`" in REWRITE
        assert "`===== materials`" in instructions
        assert f"`{MATERIALS_END}`" in instructions


class TestTheShippedFunctions:
    """``check`` and ``materials`` render through one function, with the real origin read."""

    @pytest.fixture(autouse=True)
    def _the_tree_s_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The wheel bundles instance/ as dex_engine/instance; a source
        # checkout has it at the repo root.
        monkeypatch.setattr(directive_2, "bundled_template", lambda: TEMPLATE)

    def test_the_materials_name_the_repo_origin_points_at(self, root, own_git):
        own_git(root, "init", "-q")
        own_git(root, "remote", "add", "origin", SSH)
        assert materials(root) == rendered(root, SSH)
        assert "an existing dex at someone/dex-cooking." in materials(root)

    @pytest.mark.usefixtures("own_git")
    def test_without_a_repository_the_materials_drop_the_join_section(self, root):
        assert materials(root) == rendered(root, None)

    def test_check_accepts_exactly_what_the_materials_carry(self, root, own_git):
        own_git(root, "init", "-q")
        own_git(root, "remote", "add", "origin", "https://github.com/someone/dex-cooking")
        assert check(root) == [f"`README.md` is missing: {REWRITE}"]
        readme(root, materials(root))
        assert check(root) == []
        readme(root, materials(root).replace("dex-cooking", "dex-baking"))
        assert check(root) == [NOT_THE_TEMPLATE]
