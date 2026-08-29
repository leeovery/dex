"""The instance gitattributes template: LFS by name shape, never extension list.

Driven through real ``git check-attr`` — the patterns are git's glob
dialect, and only git's own resolution proves what a line matches. The
enrichment shapes anchor on the numeric index (``media-<n>.<ext>``,
``<hash6>-asset-<n>.<ext>``) so ANY extension the source serves lands in
LFS, while the markdown that shares the directory — ``media-<n>.md``
descriptions and ``<kind>-<hash6>.md`` enrichment content — stays plain
git. ``raw/`` tracks wholesale — an exporter names assets with any
extension or none — with only the export JSON and markdown attachments
staying plain git.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from dex_engine.pipeline.types import Kind

TEMPLATE = Path(__file__).resolve().parent.parent / "instance" / "gitattributes"

ITEM = "2026-08-19-example-55ad7b"
HASH6 = "1a2b3c"

# Extensions deliberately OFF any historical allowlist (avif, webm, tar,
# and a made-up one): the name shape is what tracks them. The raw/ names
# are the exporter's own — an extensionless embed thumbnail, an .avif, a
# .jpg_large — which is why raw/ tracks wholesale, never by extension.
LFS_TRACKED = [
    f"enrichment/{ITEM}/media-0.png",
    f"enrichment/{ITEM}/media-1.webm",
    f"enrichment/{ITEM}/media-2.avif",
    f"enrichment/{ITEM}/media-12.tar",
    f"enrichment/{ITEM}/{HASH6}-asset-0.png",
    f"enrichment/{ITEM}/{HASH6}-asset-3.unknownext",
    "media/owner-shared-file.bin",
    "raw/2026/screenshot.png",
    "raw/discord/general/messages.json_Files/embed-thumbnail",
    "raw/discord/general/messages.json_Files/photo.jpg_large",
    "raw/discord/general/messages.json_Files/clip.avif",
    "raw/discord/general/messages.json_Files/report.docx",
]

# The markdown families that live in the same directories, one per real
# kind in the vocabulary plus the session's media description — none may
# ever match an LFS pattern.
PLAIN_GIT = [f"enrichment/{ITEM}/{kind.value}-{HASH6}.md" for kind in Kind] + [
    f"enrichment/{ITEM}/media-0.md",
    f"corpus/2026/{ITEM}.md",
    "state/enrichment-ledger.jsonl",
    "raw/discord/general/messages.json",
    "raw/discord/general/messages.json_Files/notes.md",
]


@pytest.fixture(scope="module")
def check_attr():
    """A ``git check-attr`` runner over a scratch repo holding the template."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")

    def run(tmp: Path, attr: str, paths: list[str]) -> dict[str, str]:
        subprocess.run([git, "init", "-q", str(tmp)], check=True, capture_output=True)  # noqa: S603
        (tmp / ".gitattributes").write_text(TEMPLATE.read_text(encoding="utf-8"))
        out = subprocess.run(  # noqa: S603 — test-built args, no shell
            [git, "-C", str(tmp), "check-attr", attr, "--", *paths],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        values: dict[str, str] = {}
        for line in out.strip().split("\n"):
            path, _, value = line.rsplit(": ", 2)
            values[path] = value
        return values

    return run


class TestEnrichmentShapes:
    def test_binaries_track_by_name_shape_whatever_the_extension(self, check_attr, tmp_path):
        values = check_attr(tmp_path, "filter", LFS_TRACKED)
        assert values == dict.fromkeys(LFS_TRACKED, "lfs")

    def test_no_markdown_or_state_file_ever_matches(self, check_attr, tmp_path):
        values = check_attr(tmp_path, "filter", PLAIN_GIT)
        assert values == dict.fromkeys(PLAIN_GIT, "unspecified")

    def test_state_jsonl_keeps_the_union_merge(self, check_attr, tmp_path):
        values = check_attr(tmp_path, "merge", ["state/enrichment-ledger.jsonl"])
        assert values == {"state/enrichment-ledger.jsonl": "union"}

    def test_exclusions_tsv_merges_as_a_union(self, check_attr, tmp_path):
        # Append-only id<TAB>reason lines, read as a set by every consumer:
        # two machines excluding between syncs must merge, not conflict.
        values = check_attr(tmp_path, "merge", ["state/exclusions.tsv"])
        assert values == {"state/exclusions.tsv": "union"}
