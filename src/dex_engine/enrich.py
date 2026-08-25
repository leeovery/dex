"""dex-enrich: the ingestion pipeline CLI.

Thin by design (§14): parse arguments, build ``Instance``/``Config``/
``Capabilities``, call the pipeline. Zero business logic lives here.
"""

import argparse
import datetime
import sys
from importlib import metadata
from pathlib import Path

from .capabilities import Capabilities
from .pipeline.registry import build_drivers
from .pipeline.run import (
    RunContext,
    compact,
    fetch_urls,
    mark,
    record_pass,
    run,
    run_transcribe,
    status_report,
)
from .pipeline.transcribe import TRANSCRIBE_RUN_CAP
from .pipeline.types import Config, Instance, Need, Status

__all__ = ["build_parser", "engine_version", "main"]


def engine_version() -> str:
    """The installed engine version — the value stamped into every ledger line.

    Raises:
        RuntimeError: The package is not installed (not a dex environment).
    """
    try:
        return metadata.version("dex-engine")
    except metadata.PackageNotFoundError as e:
        raise RuntimeError("dex-engine is not installed — run it via the bin/dex shim") from e


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: run · status · transcribe · fetch · compact · mark · pass."""
    parser = argparse.ArgumentParser(
        prog="dex-enrich",
        description="Fetch the full content behind every captured URL, ledger-driven.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="drain the work queue and report")
    run_parser.add_argument(
        "--limit", type=int, default=None, help="max units this run (big cohorts drain across runs)"
    )

    commands.add_parser(
        "status", help="ledger summary + interrupted-session backstop + capability report"
    )

    transcribe_parser = commands.add_parser(
        "transcribe", help="drain the waiting transcription cohort (§6)"
    )
    transcribe_parser.add_argument(
        "--model",
        default=None,
        help="whisper model override for this run (config transcribe_model / "
        "transcribe_api_model otherwise; drop to `small` for long backlogged queues, "
        "stay up for dense technical audio)",
    )
    transcribe_parser.add_argument(
        "--limit",
        type=int,
        default=TRANSCRIBE_RUN_CAP,
        help=f"max transcriptions this run (default {TRANSCRIBE_RUN_CAP}, §12)",
    )

    fetch_parser = commands.add_parser(
        "fetch", help="fetch extra URLs into an existing item (ledgered as children)"
    )
    fetch_parser.add_argument("item", help="the owning corpus item id")
    fetch_parser.add_argument("urls", nargs="+", help="URLs to fetch")
    fetch_parser.add_argument(
        "--parent", default=None, help="parent work-unit hash (default: the item's primary unit)"
    )
    fetch_parser.add_argument(
        "--force", action="store_true", help="exceed the per-item URL cap deliberately"
    )

    commands.add_parser("compact", help="rewrite the ledger keeping the latest line per hash")

    mark_parser = commands.add_parser(
        "mark", help="heal one ledger entry (the sanctioned correction verb)"
    )
    mark_parser.add_argument("url", help="the unit's URL")
    mark_parser.add_argument(
        "status", choices=[status.value for status in Status], help="the corrected status"
    )
    mark_parser.add_argument(
        "--reason", default=None, help="stated reason (manual/skipped need one)"
    )
    mark_parser.add_argument("--path", default=None, help="output path, for done heals")
    mark_parser.add_argument(
        "--needs",
        choices=[need.value for need in Need],
        default=None,
        help="needed capability, for waiting heals",
    )

    pass_parser = commands.add_parser(
        "pass", help="record a stage completion in state/passes.jsonl"
    )
    pass_parser.add_argument("item", help="the corpus item id")
    pass_parser.add_argument(
        "--stage", required=True, help="the completed stage (harvest / digest / wiki)"
    )

    return parser


def _dispatch(args: argparse.Namespace, ctx: RunContext) -> str:  # noqa: PLR0911 — one return per verb
    match args.command:
        case "run":
            return run(ctx, limit=args.limit)
        case "status":
            return status_report(ctx)
        case "transcribe":
            return run_transcribe(ctx, limit=args.limit)
        case "fetch":
            return fetch_urls(ctx, args.item, args.urls, parent=args.parent, force=args.force)
        case "compact":
            return compact(ctx)
        case "mark":
            return mark(
                ctx,
                args.url,
                Status(args.status),
                reason=args.reason,
                path=args.path,
                needs=Need(args.needs) if args.needs else None,
            )
        case "pass":
            return record_pass(ctx, args.item, args.stage)
        case _:
            raise RuntimeError(
                f"unreachable: argparse enforces the command set, got {args.command}"
            )


def main(argv: list[str] | None = None) -> None:
    """Parse, build the context from the instance at cwd, call the pipeline."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        config = Config.load(instance.config_path)
        # One Capabilities object feeds the drain seam, the transcribe path,
        # the file driver, and the status surface — they cannot disagree (§6).
        capabilities = Capabilities.build(config, model=getattr(args, "model", None))
        ctx = RunContext(
            instance=instance,
            config=config,
            drivers=build_drivers(capabilities=capabilities, root=instance.root),
            today=datetime.date.today,
            engine_version=engine_version(),
            provider_available=capabilities.available,
            capabilities=capabilities,
        )
        output = _dispatch(args, ctx)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"dex-enrich: {e}")
    sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
