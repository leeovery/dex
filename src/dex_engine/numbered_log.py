"""The numbered log: one ``{number, engine, date}`` record per completed number.

Migrations and directives each keep one under ``state/``, append-only and
merged as a union between machines, so one number on several lines is
expected and reads the same as one line. Records are delimited by newlines
alone: ``str.splitlines()`` would also split on unicode line separators
inside a JSON string.

The append is not atomic, and a crash mid-write can tear the record. That
window is accepted because a torn line fails :func:`read` loudly with the
file and line named, and the error states the repair.

Imports nothing from the rest of the package, so any layer may use it.
"""

import datetime
import json
from pathlib import Path

__all__ = ["append", "read"]


def read(path: Path, *, record: str, repair: str, error: type[Exception]) -> set[int]:
    """Read the log into the set of recorded numbers; a missing file is empty.

    Args:
        path: The log file.
        record: What one line records, as the errors name it
            (``"applied-migration"``).
        repair: The torn-line repair a corrupt-record error states.
        error: The exception a corrupt log raises.

    Returns:
        The recorded numbers.

    Raises:
        Exception: ``error``, when a line is not a ``{number, engine, date}``
            record. Guessing which numbers completed is how state gets
            destroyed, so a corrupt log is never skipped.
    """
    numbers: set[int] = set()
    if not path.exists():
        return numbers
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        where = f"{path}:{lineno}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise error(
                f"{where}: unparseable {record} record ({e}) — a half-written line is not "
                f"a record: {repair}"
            ) from e
        if not isinstance(raw, dict):
            raise error(f"{where}: record must be a JSON object: {line!r}")
        number = raw.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise error(
                f"{where}: record must carry an integer 'number' ({{number, engine, date}}): "
                f"{line!r}"
            )
        numbers.add(number)
    return numbers


def append(path: Path, *, number: int, engine: str, date: datetime.date) -> None:
    """Append one ``{number, engine, date}`` record, creating the file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"number": number, "engine": engine, "date": date.isoformat()})
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
