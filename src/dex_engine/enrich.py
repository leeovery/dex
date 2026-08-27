"""dex-enrich: the ingestion pipeline CLI.

Thin by design: parse arguments, build ``Instance``/``Config``/
``Capabilities``, call the pipeline. Zero business logic lives here.
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

from .capabilities import Capabilities
from .pipeline.capture import item_new
from .pipeline.digest import item_digest
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
from .version import engine_version

__all__ = ["build_parser", "engine_version", "main"]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: run · status · transcribe · fetch · compact · mark · pass · item."""
    parser = argparse.ArgumentParser(
        prog="dex-enrich",
        description="Fetch the full content behind every captured URL, ledger-driven.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="drain the work queue and report")
    run_parser.add_argument(
        "--limit", type=int, default=None, help="max units this run (big cohorts drain across runs)"
    )

    status_parser = commands.add_parser(
        "status", help="ledger summary + interrupted-session backstop + capability report"
    )
    status_parser.add_argument(
        "--item",
        default=None,
        help="show one item's ledger view instead: every unit it owns, with "
        "provenance and outputs (what was fetched is a ledger fact)",
    )

    transcribe_parser = commands.add_parser(
        "transcribe", help="drain the waiting transcription cohort"
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
        help=f"max transcriptions this run (default {TRANSCRIBE_RUN_CAP})",
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

    item_parser = commands.add_parser(
        "item", help="per-item file writes — code writes the shape, judgment supplies the values"
    )
    item_commands = item_parser.add_subparsers(dest="item_command", required=True)
    new_parser = item_commands.add_parser(
        "new", help="create the corpus item for a capture file (id rules + provenance)"
    )
    new_parser.add_argument("capture", type=Path, help="the capture file in inbox/")
    new_parser.add_argument(
        "--slug",
        default=None,
        help="judgment's slug for the id (default: derived from the note, then the URL)",
    )
    new_parser.add_argument(
        "--shared-by",
        dest="shared_by",
        default="owner",
        help="provenance display name (default: owner)",
    )
    digest_parser = item_commands.add_parser(
        "digest",
        help="write an item's digest from a JSON payload (judgment in, shape decided) "
        "and record the digest pass",
    )
    digest_parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="the JSON payload file (conventionally under cache/): "
        '{"id", "signal", "topics", "facts"}, entities optional',
    )

    return parser


def _dispatch(args: argparse.Namespace, ctx: RunContext) -> str:  # noqa: PLR0911 — one return per verb
    match args.command:
        case "run":
            return run(ctx, limit=args.limit)
        case "status":
            return status_report(ctx, item_id=args.item)
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
        case "item" if args.item_command == "digest":
            return item_digest(args.file, ctx=ctx)
        case "item":
            return item_new(
                args.capture,
                instance=ctx.instance,
                drivers=ctx.drivers,
                today=ctx.today,
                slug=args.slug,
                shared_by=args.shared_by,
            )
        case _:
            raise RuntimeError(
                f"unreachable: argparse enforces the command set, got {args.command}"
            )


def _load_env(root: Path) -> None:
    """Fold the instance's gitignored ``.env`` into the environment.

    The local-secret slot: keys (``OPENAI_API_KEY`` for whisper-api) live
    here rather than in the committed config. Simple ``KEY=VALUE`` lines,
    ``#`` comments and malformed lines skipped; one layer of surrounding
    quotes stripped (``KEY="sk-…"`` is how a shell-shaped file is written,
    and a literal quote in the value 401s every call); setdefault
    semantics — a real environment variable always wins.

    Raises:
        OSError: The file exists but cannot be read (a directory, say).
        UnicodeDecodeError: The file is not UTF-8.
    """
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip():  # a nameless `=value` line would crash setdefault
            os.environ.setdefault(key.strip(), _unquoted(value.strip()))


def _unquoted(value: str) -> str:
    """One layer of matched surrounding quotes off a ``.env`` value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":  # noqa: PLR2004 — a quoted value is two quotes plus content
        return value[1:-1]
    return value


def main(argv: list[str] | None = None) -> None:
    """Parse, build the context from the instance at cwd, call the pipeline."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        # Inside the wrapper: an unreadable .env is instance state like any
        # other, and it must fail as a stated line, not a traceback.
        _load_env(instance.root)
        config = Config.load(instance.config_path)
        # One Capabilities object feeds the drain seam, the transcribe path,
        # the file driver, and the status surface — they cannot disagree.
        capabilities = Capabilities.build(config, model=getattr(args, "model", None))
        ctx = RunContext(
            instance=instance,
            config=config,
            drivers=build_drivers(capabilities=capabilities, root=instance.root, config=config),
            today=datetime.date.today,
            now=lambda: datetime.datetime.now(datetime.UTC),
            engine_version=engine_version(),
            provider_available=capabilities.available,
            capabilities=capabilities,
            command=f"enrich {args.command}",
        )
        output = _dispatch(args, ctx)
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-enrich: {e}")
    sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
