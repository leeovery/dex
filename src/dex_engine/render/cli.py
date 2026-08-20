"""dex-render: emit a named surface verbatim from a JSON payload (§11).

The cognitive call path: Claude writes a JSON payload — including free-prose
fields like notes — to ``cache/``, runs ``bin/dex render --file <payload>``,
and emits the result verbatim. Layout that is fully determined by data is
computed here, never re-derived character-by-character by the model; even
prose position and framing stay deterministic.

The payload file is one JSON object naming the surface and its payload::

    {"surface": "ingest-receipt", "payload": {...}}

Engine-internal reports (``enrich run``, ``sync``) render in-process and
never pass through this command.
"""

import argparse
import json
import sys
from pathlib import Path

from .surfaces import render

__all__ = ["build_parser", "main", "render_file"]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-render takes one payload file."""
    parser = argparse.ArgumentParser(
        prog="dex-render",
        description="Render a named surface from a JSON payload file "
        '({"surface": name, "payload": {...}}) and print it verbatim (§11).',
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="the JSON payload file (conventionally under cache/)",
    )
    return parser


def render_file(path: Path) -> str:
    """Render the surface a payload file names.

    Args:
        path: A JSON file of the form ``{"surface": str, "payload": object}``.

    Returns:
        The rendered report.

    Raises:
        ValueError: The file is not JSON, lacks the two keys, or the payload
            does not conform to the named surface's shape.
        OSError: The file cannot be read.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - {"surface", "payload"})
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown} — expected surface and payload")
    surface = raw.get("surface")
    if not isinstance(surface, str) or not surface:
        raise ValueError(f"{path}: surface must be a non-empty string")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: payload must be a JSON object")
    return render(surface, payload)


def main(argv: list[str] | None = None) -> None:
    """Parse, render, print — zero business logic (§14)."""
    args = build_parser().parse_args(argv)
    try:
        output = render_file(args.file)
    except (OSError, ValueError) as e:
        sys.exit(f"dex-render: {e}")
    sys.stdout.write(output if output.endswith("\n") else output + "\n")


if __name__ == "__main__":
    main()
