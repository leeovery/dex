"""dex-new: scaffold a new dex instance from the engine's bundled template.

``uvx --from git+https://github.com/leeovery/dex dex-new <name>``

Creates ``./<name>``: the directory tree (tracked dirs plus the gitignored
``cache/``), seed CLAUDE.md and README.md (both to be personalized), the
engine-managed machinery (via the same template sync every instance runs),
git init, and local LFS.
"""

import argparse
import subprocess
import sys
from collections.abc import Callable
from importlib.resources.abc import Traversable
from pathlib import Path

from .sync import sync
from .template import bundled_template

__all__ = ["EPHEMERAL", "SEEDS", "TREE", "build_parser", "main", "scaffold"]

# Gitignored, so it carries no .gitkeep — but it must exist from birth: the
# per-item procedure renders every receipt through `cache/receipt.json`, and
# a fresh instance would otherwise fail its first documented render step.
EPHEMERAL = ["cache"]

TREE = [
    "corpus",
    "enrichment",
    "wiki/topics",
    "wiki/entities",
    "wiki/syntheses",
    "state/digests",
    "raw",
    "bin",
    "media",
    "inbox",
]

SEEDS = {
    ".gitignore": ".DS_Store\n.env\ncache/\n",
    # Instance config: config.json from birth — migration 1 renames the
    # old normalize-config.json in pre-rewrite instances; new ones never
    # carry the old name.
    "state/config.json": '{\n  "internal_domains": []\n}\n',
    "wiki/index.md": "# Index\n\nNo pages yet — first ingest pending.\n",
    "wiki/log.md": "# Ops log\n",
    "wiki/pins.md": "# Pins\n\nHuman corrections as claim+anchor; regeneration must "
    "re-apply these.\n",
}


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(  # noqa: S603 — engine-built args, no shell
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def scaffold(
    root: Path,
    *,
    run: Callable[[list[str], Path], None] = _run,
    template: Traversable | None = None,
) -> list[str]:
    """Build a new instance at ``root``.

    Args:
        root: The instance directory to create (must be absent or empty).
        run: The subprocess seam (git init, git lfs install) — injected so
            tests are hermetic.
        template: Template override for tests; ``None`` uses the wheel's
            bundled ``instance/`` tree.

    Returns:
        The next-step lines for the operator.

    Raises:
        ValueError: ``root`` exists and is not empty.
    """
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"{root} exists and is not empty")
    if template is None:
        template = bundled_template()
    for directory in TREE:
        (root / directory).mkdir(parents=True, exist_ok=True)
        # Empty dirs don't survive git clone — keep the tree shape tracked.
        (root / directory / ".gitkeep").write_text("")
    for directory in EPHEMERAL:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for rel, content in SEEDS.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    (root / "CLAUDE.md").write_text((template / "CLAUDE.md").read_text(encoding="utf-8"))
    (root / "README.md").write_text((template / "README.md").read_text(encoding="utf-8"))
    sync(root, template=template)
    run(["git", "init", "-q"], root)
    run(["git", "lfs", "install", "--local"], root)
    # The next steps stay neutral on hosting: the GitHub question is the
    # owner's, answered during setup, and an unconditional `gh repo create`
    # here read as an instruction to an owner who declined GitHub.
    return [
        f"created {root}",
        "next: personalize CLAUDE.md and README.md, commit, then:",
        f"  if using GitHub: gh repo create {root.name} --private --source . --push",
        "  bin/dex inbox ensure",
    ]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-new <name>."""
    parser = argparse.ArgumentParser(
        prog="dex-new",
        description="Scaffold a new dex instance from the engine's bundled template.",
    )
    parser.add_argument("name", help="the instance directory to create under cwd")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, scaffold under cwd, print the next steps."""
    args = build_parser().parse_args(argv)
    try:
        lines = scaffold(Path.cwd() / args.name)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        sys.exit(f"dex-new: {e}")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
