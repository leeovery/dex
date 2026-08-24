"""dex-issue: file a session-observed engine bug at the public repo.

Thin by design: parse ``--file``, build ``Instance``/``Config``, call the
pipeline (:func:`~dex_engine.pipeline.observed.report_observed`). Zero
business logic lives here.
"""

import argparse
import datetime
import sys
from pathlib import Path

from .pipeline import issues
from .pipeline.observed import report_observed
from .pipeline.types import Config, Instance
from .version import engine_version

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-issue --file <payload.json>."""
    parser = argparse.ArgumentParser(
        prog="dex-issue",
        description="File a session-observed engine bug upstream: structured mechanics "
        "only, and a payload naming instance content is refused, never scrubbed.",
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="the JSON payload file (conventionally under cache/): "
        '{"verb", "expected", "observed"}, "steps" and the local-only "note" optional',
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, build the context from the instance at cwd, call the pipeline."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        config = Config.load(instance.config_path)
        output = report_observed(
            args.file,
            enabled=config.report_issues,
            state_dir=instance.state_dir,
            engine_version=engine_version(),
            today=datetime.date.today,
            gh=issues.gh_runner,
        )
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-issue: {e}")
    sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
