"""Tests for seeds.py: the README and the seed lens, rendered for one instance."""

from pathlib import Path

import pytest

from dex_engine.seeds import (
    GENERAL_LINE,
    LENS_LINE,
    LENS_PLACEHOLDER,
    NAME_PLACEHOLDER,
    REPO_PLACEHOLDER,
    instance_readme,
    lens,
    readme,
)

# The repo's template tree — what the wheel bundles as dex_engine/instance.
TEMPLATE = Path(__file__).resolve().parent.parent / "instance"
START = Path(__file__).resolve().parent.parent / "docs" / "start.md"
LENS_LINK = "(./lens.md)"
JOIN_HEADING = "## Run it on another machine"
PROMPT_TAIL = "this is an existing dex at {repo}."


def template_readme() -> str:
    return (TEMPLATE / "README.md").read_text(encoding="utf-8")


def origin_of(url: str | None):
    """A git seam answering ``url`` for whatever root it is asked about."""
    return lambda _root: url


def template_with(tmp_path: Path, readme_text: str) -> Path:
    tree = tmp_path / "template"
    tree.mkdir()
    (tree / "README.md").write_text(readme_text, encoding="utf-8")
    return tree


def with_lens(root: Path) -> Path:
    """``root`` made an instance holding a ``lens.md``, as dex-new leaves one."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lens.md").write_text("# dex-cooking\n\nWeeknight recipes.\n", encoding="utf-8")
    return root


class TestLens:
    def test_the_seed_is_titled_with_the_name_and_otherwise_unchanged(self):
        seed = (TEMPLATE / "lens.md").read_text(encoding="utf-8")
        assert seed.startswith(f"# {NAME_PLACEHOLDER}\n")
        assert lens(TEMPLATE, "dex-cooking") == "# dex-cooking\n" + seed.split("\n", 1)[1]


class TestReadme:
    def test_the_new_instance_readme_is_named_links_the_lens_and_keeps_the_repo(self):
        # dex-new always seeds lens.md, so its README always links it.
        text = readme(TEMPLATE, "dex-cooking")
        assert text == template_readme().replace(NAME_PLACEHOLDER, "dex-cooking").replace(
            LENS_PLACEHOLDER, LENS_LINE
        )
        assert text.startswith("# dex-cooking\n")
        assert NAME_PLACEHOLDER not in text
        assert LENS_PLACEHOLDER not in text
        assert PROMPT_TAIL.format(repo=REPO_PLACEHOLDER) in text

    def test_the_template_holds_the_lens_line_once_as_a_paragraph_of_its_own(self):
        lines = template_readme().splitlines()
        [at] = [n for n, line in enumerate(lines) if LENS_PLACEHOLDER in line]
        assert lines[at - 1 : at + 2] == ["", LENS_PLACEHOLDER, ""]

    def test_the_template_holds_the_repo_once_inside_the_join_section_it_ends_on(self):
        lines = template_readme().splitlines()
        [holding] = [n for n, line in enumerate(lines) if REPO_PLACEHOLDER in line]
        join = lines.index(JOIN_HEADING)
        assert join < holding
        assert [line for line in lines[join:] if line.startswith("#")] == [JOIN_HEADING]


class TestInstanceReadme:
    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:someone/dex-cooking.git",
            "git@github.com:someone/dex-cooking",
            "https://github.com/someone/dex-cooking.git",
            "https://github.com/someone/dex-cooking",
        ],
        ids=["ssh", "ssh-bare", "https", "https-bare"],
    )
    def test_a_github_origin_fills_the_join_prompt(self, tmp_path, url):
        root = with_lens(tmp_path / "dex-cooking")
        text = instance_readme(TEMPLATE, root, origin=origin_of(url))
        assert text == readme(TEMPLATE, "dex-cooking").replace(
            REPO_PLACEHOLDER, "someone/dex-cooking"
        )
        assert PROMPT_TAIL.format(repo="someone/dex-cooking") in text
        assert REPO_PLACEHOLDER not in text

    @pytest.mark.parametrize(
        "url",
        [None, "git@gitlab.com:someone/dex-cooking.git", "/srv/git/dex-cooking.git"],
        ids=["no-origin", "elsewhere", "local-path"],
    )
    def test_without_a_github_origin_the_join_section_goes_whole(self, tmp_path, url):
        root = with_lens(tmp_path / "dex-cooking")
        text = instance_readme(TEMPLATE, root, origin=origin_of(url))
        before_join = readme(TEMPLATE, "dex-cooking").split(f"\n{JOIN_HEADING}\n")[0]
        assert text == before_join.rstrip("\n") + "\n"
        assert JOIN_HEADING not in text
        assert REPO_PLACEHOLDER not in text
        assert "[`lens.md`](./lens.md)" in text
        # The closing line sits above the join section, so it stays.
        assert text.endswith(
            "\nThe machinery lives in the shared engine and never needs touching.\n"
        )

    def test_the_name_is_the_root_directory_s(self, tmp_path):
        text = instance_readme(TEMPLATE, tmp_path / "dex-garden", origin=origin_of(None))
        assert text.startswith("# dex-garden\n")

    def test_the_seam_is_asked_about_the_instance_root(self, tmp_path):
        asked: list[Path] = []

        def origin(root: Path) -> str | None:
            asked.append(root)
            return None

        instance_readme(TEMPLATE, tmp_path / "dex-cooking", origin=origin)
        assert asked == [tmp_path / "dex-cooking"]


class TestTheLensLine:
    """A README links lens.md only when the instance has one, never a dead link."""

    def test_an_instance_with_a_lens_links_it(self, tmp_path):
        root = with_lens(tmp_path / "dex-cooking")
        text = instance_readme(TEMPLATE, root, origin=origin_of(None))
        assert f"\n\n{LENS_LINE}\n\n" in text
        assert GENERAL_LINE not in text

    def test_an_instance_with_no_lens_is_a_general_knowledge_dex_with_no_link(self, tmp_path):
        root = tmp_path / "dex-cooking"
        root.mkdir()
        text = instance_readme(TEMPLATE, root, origin=origin_of(None))
        assert f"\n\n{GENERAL_LINE}\n\n" in text
        assert LENS_LINK not in text
        assert LENS_PLACEHOLDER not in text

    def test_the_two_renders_differ_only_in_the_lens_line(self, tmp_path):
        bare = tmp_path / "dex-cooking"
        bare.mkdir()
        general = instance_readme(TEMPLATE, bare, origin=origin_of(None))
        linked = instance_readme(TEMPLATE, with_lens(bare), origin=origin_of(None))
        assert general.replace(GENERAL_LINE, LENS_LINE) == linked

    def test_an_empty_lens_md_is_still_a_file_to_link(self, tmp_path):
        root = tmp_path / "dex-cooking"
        root.mkdir()
        (root / "lens.md").write_text("", encoding="utf-8")
        assert LENS_LINE in instance_readme(TEMPLATE, root, origin=origin_of(None))

    def test_only_the_instance_s_own_lens_md_counts(self, tmp_path):
        (tmp_path / "lens.md").write_text("a lens beside the instance\n", encoding="utf-8")
        root = tmp_path / "dex-cooking"
        root.mkdir()
        assert GENERAL_LINE in instance_readme(TEMPLATE, root, origin=origin_of(None))

    def test_the_setup_guide_gives_the_general_line_exactly(self):
        # docs/start.md tells setup to swap the line in by hand when the
        # owner deletes the seeded lens; its text must be the render's.
        guide = START.read_text(encoding="utf-8")
        block = ["```", *GENERAL_LINE.split("\n"), "```"]
        assert "\n".join(f"  {line}" for line in block) in guide
        replaced = "What this dex reads for is its lens:"
        assert f"`{replaced}`" in guide
        assert LENS_LINE.startswith(replaced)


class TestDroppingTheJoinSection:
    """The section holding the repo, cut to the next heading as high as its own."""

    def render(self, tmp_path: Path, text: str) -> str:
        template = template_with(tmp_path, text)
        return instance_readme(template, tmp_path / "dex-x", origin=origin_of(None))

    def test_a_section_after_it_is_kept_with_its_blank_line(self, tmp_path):
        text = "# t\n\nIntro.\n\n## Join\n\nat <owner>/<repo>\n\n## After\n\nKept.\n"
        assert self.render(tmp_path, text) == "# t\n\nIntro.\n\n## After\n\nKept.\n"

    def test_its_subsections_go_with_it(self, tmp_path):
        text = "# t\n\n## Join\n\nat <owner>/<repo>\n\n### Detail\n\nGone.\n\n## After\n\nKept.\n"
        assert self.render(tmp_path, text) == "# t\n\n## After\n\nKept.\n"

    def test_a_higher_heading_ends_it(self, tmp_path):
        text = "# t\n\n## Join\n\nat <owner>/<repo>\n\n# Next\n\nKept.\n"
        assert self.render(tmp_path, text) == "# t\n\n# Next\n\nKept.\n"

    def test_a_subsection_holding_the_repo_goes_alone(self, tmp_path):
        text = "# t\n\n## Use\n\nKept.\n\n### Join\n\nat <owner>/<repo>\n\n### More\n\nKept too.\n"
        assert self.render(tmp_path, text) == "# t\n\n## Use\n\nKept.\n\n### More\n\nKept too.\n"

    def test_a_heading_right_above_the_repo_opens_the_section(self, tmp_path):
        text = "# t\n\nIntro.\n\n## Join\nat <owner>/<repo>\n"
        assert self.render(tmp_path, text) == "# t\n\nIntro.\n"

    def test_a_heading_holding_the_repo_is_its_own_section(self, tmp_path):
        text = "# t\n\n## Join <owner>/<repo>\n## After\n\nKept.\n"
        assert self.render(tmp_path, text) == "# t\n\n## After\n\nKept.\n"

    def test_trailing_spaces_on_the_last_kept_line_stay(self, tmp_path):
        # Two trailing spaces are a markdown line break: only the section goes.
        text = "# t\n\nIntro.  \n\n## Join\n\nat <owner>/<repo>\n"
        assert self.render(tmp_path, text) == "# t\n\nIntro.  \n"

    def test_the_heading_nearest_above_the_repo_opens_the_section(self, tmp_path):
        text = "# t\n\n## Use\n\nKept.\n\n## Join\n\nLead.\n\nat <owner>/<repo>\n"
        assert self.render(tmp_path, text) == "# t\n\n## Use\n\nKept.\n"

    def test_the_text_ends_in_exactly_one_newline(self, tmp_path):
        text = "# t\n\nIntro.\n\n\n## Join\n\nat <owner>/<repo>\n\n\n"
        assert self.render(tmp_path, text) == "# t\n\nIntro.\n"

    def test_a_hash_without_a_space_is_not_a_heading(self, tmp_path):
        text = "# t\n\n## Join\n\n#hashtag\n\nat <owner>/<repo>\n"
        assert self.render(tmp_path, text) == "# t\n"

    def test_a_template_with_no_join_prompt_renders_unchanged(self, tmp_path):
        text = "# <instance name>\n\nNo prompt here.\n"
        assert self.render(tmp_path, text) == "# dex-x\n\nNo prompt here.\n"
