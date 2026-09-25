"""dex-directive: list, show and complete the directives the engine ships.

``bin/dex directive list`` names the directives this instance has not
completed, in the order they run. ``show <n>`` prints one directive's intent
and instructions for the session to perform. ``done <n>`` runs the
directive's check and appends its record to ``state/directives.jsonl`` only
when every condition holds. Directives complete in numeric order, so one
whose predecessor is still pending is refused.
"""

import argparse
import datetime
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .directives import Directive, append_done, discover, log_path, pending
from .pipeline.types import Instance
from .render import kernel
from .version import engine_version

__all__ = ["build_parser", "complete", "list_pending", "main", "show"]


def list_pending(instance: Instance, shipped: Sequence[Directive]) -> str:
    """The pending directives with their intents, in the order they run.

    Args:
        instance: The instance.
        shipped: The directives the running engine ships.

    Returns:
        One line per pending directive under a count, or a line saying none are.
    """
    waiting = pending(instance.root, shipped)
    if not waiting:
        return "no directives pending"
    count = kernel.plural(len(waiting), "directive")
    return "\n".join([f"{count} pending, to perform in this order:", *(_title(d) for d in waiting)])


def show(shipped: Sequence[Directive], number: int) -> str:
    """One directive's intent and its full instructions.

    Raises:
        ValueError: The engine ships no directive with that number.
    """
    directive = _shipped(shipped, number)
    return f"{_title(directive)}\n\n{directive.instructions}"


def complete(
    instance: Instance,
    number: int,
    *,
    shipped: Sequence[Directive],
    engine: str,
    today: Callable[[], datetime.date],
) -> str:
    """Record directive ``number`` as done once its check passes.

    Args:
        instance: The instance.
        number: The directive the session performed.
        shipped: The directives the running engine ships.
        engine: The running engine's version, stamped into the record.
        today: Injected date clock, read only when the record is written.

    Returns:
        The confirmation, or the statement that it was already recorded
        (nothing is written then).

    Raises:
        ValueError: The number names no shipped directive, an earlier
            directive is still pending, or the check found unmet conditions,
            each of which the message lists. Nothing is written.
    """
    directive = _shipped(shipped, number)
    waiting = pending(instance.root, shipped)
    if number not in {candidate.number for candidate in waiting}:
        return f"directive {number} is already recorded as done; nothing written"
    first = waiting[0]
    if first.number != number:
        raise ValueError(
            f"directive {number} cannot complete while directive {first.number} is pending "
            f"({first.intent}); directives complete in numeric order, so directive "
            f"{first.number} comes first"
        )
    unmet = directive.check(instance.root)
    if unmet:
        found = (
            f"directive {number} is not done: its check found "
            f"{kernel.plural(len(unmet), 'unmet condition')}, and nothing was recorded"
        )
        raise ValueError("\n".join([found, *(f"- {condition}" for condition in unmet)]))
    append_done(log_path(instance.root), number=number, engine=engine, date=today())
    return (
        f"directive {number} recorded in state/directives.jsonl; commit that record and the "
        f'directive\'s edits together, message "directive {number}: {directive.intent}"'
    )


def _shipped(shipped: Sequence[Directive], number: int) -> Directive:
    for directive in shipped:
        if directive.number == number:
            return directive
    known = ", ".join(str(directive.number) for directive in shipped) or "none"
    raise ValueError(f"no directive {number} ships with this engine (shipped: {known})")


def _title(directive: Directive) -> str:
    return f"directive {directive.number}: {directive.intent}"


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-directive list | show <n> | done <n>."""
    parser = argparse.ArgumentParser(
        prog="dex-directive",
        description="The engine's directives for this instance: judgment work performed "
        "by the run after the pull, each recorded once its check passes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the pending directives, in the order they run")
    show_parser = commands.add_parser("show", help="one directive's intent and instructions")
    show_parser.add_argument("number", type=int, help="the directive's number")
    done_parser = commands.add_parser(
        "done",
        help="run the directive's check and record it in state/directives.jsonl when "
        "every condition holds",
    )
    done_parser.add_argument("number", type=int, help="the directive's number")
    return parser


def _dispatch(args: argparse.Namespace, instance: Instance, shipped: list[Directive]) -> str:
    match args.command:
        case "list":
            return list_pending(instance, shipped)
        case "show":
            return show(shipped, args.number)
        case "done":
            return complete(
                instance,
                args.number,
                shipped=shipped,
                engine=engine_version(),
                today=datetime.date.today,
            )
        case _:
            raise RuntimeError(
                f"unreachable: argparse enforces the command set, got {args.command}"
            )


def main(argv: list[str] | None = None) -> None:
    """Parse, discover the shipped directives, run the verb against the instance at cwd."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        output = _dispatch(args, instance, discover())
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-directive: {e}")
    sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
