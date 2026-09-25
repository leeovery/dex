"""dex-new: scaffold a new dex instance from the engine's bundled template.

``uvx --from git+https://github.com/leeovery/dex dex-new <name>``

Creates ``./<name>``: the directory tree (tracked dirs plus the gitignored
``cache/``), the README and ``lens.md`` seeded with the instance's name (the
lens to be filled in), the engine-managed machinery, CLAUDE.md among it (via
the same template sync every instance runs), every shipped directive
recorded as done, git init, and local LFS.
"""

import argparse
import datetime
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib.resources.abc import Traversable
from pathlib import Path

from .directives import Directive, record_shipped
from .sync import sync
from .template import bundled_template
from .version import engine_version

__all__ = ["EPHEMERAL", "NAMED_SEEDS", "SEEDS", "TREE", "build_parser", "main", "scaffold"]

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

# Template files written once, with the instance's name in place of the
# placeholder, and the owner's from then on: sync never touches either.
NAMED_SEEDS = ("README.md", "lens.md")
_NAME_PLACEHOLDER = "<instance name>"


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(  # noqa: S603 — engine-built args, no shell
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def scaffold(  # noqa: PLR0913 — the seams are the signature: subprocess, template, directives, clocks
    root: Path,
    *,
    run: Callable[[list[str], Path], None] = _run,
    template: Traversable | None = None,
    shipped: Sequence[Directive] | None = None,
    today: Callable[[], datetime.date] = datetime.date.today,
    version: Callable[[], str] = engine_version,
) -> list[str]:
    """Build a new instance at ``root``.

    Args:
        root: The instance directory to create (must be absent or empty).
        run: The subprocess seam (git init, git lfs install) — injected so
            tests are hermetic.
        template: Template override for tests; ``None`` uses the wheel's
            bundled ``instance/`` tree.
        shipped: Directive-set override for tests; ``None`` records the
            engine's own directives as done.
        today: Injected date clock, for the directive records.
        version: The running engine's version, for the directive records.

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
    for rel in NAMED_SEEDS:
        seed = (template / rel).read_text(encoding="utf-8")
        (root / rel).write_text(seed.replace(_NAME_PLACEHOLDER, root.name), encoding="utf-8")
    sync(root, template=template)
    record_shipped(root, engine=version(), date=today(), shipped=shipped)
    run(["git", "init", "-q"], root)
    run(["git", "lfs", "install", "--local"], root)
    # The next steps stay neutral on hosting: the GitHub question is the
    # owner's, answered during setup, and an unconditional `gh repo create`
    # here read as an instruction to an owner who declined GitHub.
    return [
        f"created {root}",
        (
            "next: fill in lens.md (what this dex reads for) and README.md's <owner>/<repo>, "
            "commit, then:"
        ),
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
