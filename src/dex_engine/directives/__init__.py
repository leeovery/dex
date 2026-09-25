"""Directives: judgment work the engine ships for the run session to perform.

A migration is mechanical by rule and never transforms content, so work on
an instance's own files that needs reading and deciding, such as where each
part of an owner's prose belongs, cannot be one. A directive states that
work once, and every instance performs it on its next run, unattended, and
records that it did.

One module per directive, ``directive_<n>.py``: plain integers in a sequence
of their own, discovered in numeric order, with the instructions beside the
module as ``directive_<n>.md``. The wheel bundles the markdown with the
module and discovery reads it through importlib.resources. A module defines:

- ``INTENT``: one line, printed on the sync report and by ``directive list``.
- ``check(root) -> list[str]``: every condition the instance at ``root``
  does not meet yet, empty once the directive is done. It confirms only
  what code can see (a file exists and is not empty, config still parses,
  a link is present); whether the work was faithful rests on the
  instructions and on rehearsing the directive over real instances before
  it is released.

The completed log is ``state/directives.jsonl``, one ``{number, engine,
date}`` record per directive: appended by ``directive done`` only after the
check passes, and seeded with every shipped number when ``dex-new`` creates
an instance, which is born in the shape directives exist to reach.

Authoring rules. A directive does one job over one source and accounts for
all of it. It is instance-blind: it never names an instance, an owner or a
path outside the instance, and it copes with shapes nobody maintaining the
engine has seen. It exists only for work that needs judgment, because
whatever code can do safely is a migration. It is always completable from
the instance's own files, state and git history without the owner, and an
instance with nothing to do for it completes it by finding nothing to do.
Content is moved, never silently lost: text removed without a new home is
named in the directive's commit message, and git history keeps it.
"""

import datetime
import pkgutil
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module, resources
from pathlib import Path

from dex_engine import numbered_log

__all__ = [
    "Directive",
    "DirectiveError",
    "append_done",
    "discover",
    "log_path",
    "pending",
    "read_done",
    "record_shipped",
]


class DirectiveError(RuntimeError):
    """A directive, or the completed-directives log, is in a state code cannot fix."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Directive:
    """One shipped directive: its number, intent, instructions and check."""

    number: int
    intent: str
    instructions: str
    check: Callable[[Path], list[str]]


_MODULE_RE = re.compile(r"^directive_([1-9][0-9]*)$")

_TORN_REPAIR = (
    "delete the torn line and re-run; a directive it recorded reads as pending again, and "
    "performing it again finds nothing left to do"
)


def log_path(root: Path) -> Path:
    """``state/directives.jsonl`` under the instance at ``root``."""
    return root / "state" / "directives.jsonl"


def discover(package: str = __name__) -> list[Directive]:
    """Discover the directives ``package`` ships, in ascending numeric order.

    The ``directive_<n>`` module names are the registry: the number comes
    from the filename, so no module can disagree with it.

    Args:
        package: The package to search; tests point it at a fixture set.

    Returns:
        Every directive the package ships, ascending by number.

    Raises:
        DirectiveError: A module name looks like a directive but is not
            ``directive_<n>`` with a plain integer, or a directive lacks its
            intent, its check or its instructions. Each is a packaging bug,
            and a loud one, because a directive discovery skips never runs.
    """
    found: list[Directive] = []
    for module_info in pkgutil.iter_modules(import_module(package).__path__):
        match = _MODULE_RE.match(module_info.name)
        if match:
            found.append(_load(package, module_info.name, int(match.group(1))))
        elif module_info.name.startswith("directive"):
            raise DirectiveError(
                f"module {module_info.name!r} looks like a directive but does not match "
                "directive_<n> with a plain integer; rename it or it will never be discovered"
            )
    return _ascending(found)


def _load(package: str, name: str, number: int) -> Directive:
    module = import_module(f"{package}.{name}")
    intent = getattr(module, "INTENT", None)
    if not isinstance(intent, str) or not intent.strip() or "\n" in intent:
        raise DirectiveError(
            f"{module.__name__} must define INTENT as one non-empty line: the sync report "
            "and `directive list` print it"
        )
    check = getattr(module, "check", None)
    if not callable(check):
        raise DirectiveError(f"{module.__name__} must define check(root) -> list[str]")
    source = resources.files(package) / f"{name}.md"
    instructions = source.read_text(encoding="utf-8") if source.is_file() else ""
    if not instructions.strip():
        raise DirectiveError(
            f"{module.__name__} ships no instructions: {name}.md beside it is missing or empty"
        )
    return Directive(number=number, intent=intent, instructions=instructions, check=check)


def read_done(path: Path) -> set[int]:
    """Read the completed-directives log into the set of completed numbers.

    Two machines that both performed a directive union-merge to two lines
    for one number, which collapse here; a missing file is an empty log.

    Args:
        path: The ``state/directives.jsonl`` file.

    Returns:
        The completed directive numbers.

    Raises:
        DirectiveError: A line is not a ``{number, engine, date}`` record.
    """
    return numbered_log.read(
        path, record="completed-directive", repair=_TORN_REPAIR, error=DirectiveError
    )


def append_done(path: Path, *, number: int, engine: str, date: datetime.date) -> None:
    """Append one ``{number, engine, date}`` record, creating the file if needed."""
    numbered_log.append(path, number=number, engine=engine, date=date)


def pending(root: Path, shipped: Sequence[Directive] | None = None) -> list[Directive]:
    """The shipped directives the instance at ``root`` has not completed.

    Args:
        root: The instance root.
        shipped: Override for tests; ``None`` discovers the engine's own set.

    Returns:
        The pending directives, ascending by number: the order they run in.

    Raises:
        DirectiveError: The log is corrupt, or discovery met a packaging bug.
    """
    done = read_done(log_path(root))
    return _ascending(
        directive for directive in _or_discovered(shipped) if directive.number not in done
    )


def record_shipped(
    root: Path,
    *,
    engine: str,
    date: datetime.date,
    shipped: Sequence[Directive] | None = None,
) -> None:
    """Record every shipped directive as done, the way ``dex-new`` seeds an instance.

    A fresh instance is born in the shape the directives exist to reach, so
    performing them would spend a session converting nothing. With nothing
    shipped nothing is written: a missing log already reads as empty, and
    the first record creates the file.

    Args:
        root: The instance root.
        engine: The running engine's version, stamped into each record.
        date: The date each record carries.
        shipped: Override for tests; ``None`` discovers the engine's own set.
    """
    path = log_path(root)
    for directive in _or_discovered(shipped):
        append_done(path, number=directive.number, engine=engine, date=date)


def _or_discovered(shipped: Sequence[Directive] | None) -> Sequence[Directive]:
    return discover() if shipped is None else shipped


def _ascending(directives: Iterable[Directive]) -> list[Directive]:
    return sorted(directives, key=lambda directive: directive.number)
