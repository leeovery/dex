"""The csv-builtin extractor: stdlib csv → markdown table, zero deps.

Exists so CSV extraction never depends on the anydoc wheel: the Format is
the contract, and CSV falls back here independently if anydoc dies.
"""

import csv
import io

from dex_engine.pipeline.classify import ProviderInputError
from dex_engine.pipeline.types import Availability, Extraction, Format

__all__ = ["CsvBuiltinExtractor"]

_SNIFF_SAMPLE_CHARS = 4096
_FALLBACK_DELIMITERS = ",;\t|"

# The stdlib's 128KB-per-field default trips on an embedded JSON blob,
# which is a big cell in a fine file, not a broken one — so the limit is
# raised. Not removed: one unterminated quote in a file that only looked
# like CSV makes the whole file a single field, and a bound turns that
# into a stated `manual` instead of eating the machine's memory.
_MAX_FIELD_CHARS = 16 * 1024 * 1024


class CsvBuiltinExtractor:
    """Render a CSV as a markdown table; the first row is the header."""

    name: str = "csv-builtin"

    def supports(self, fmt: Format) -> bool:
        """CSV only — every other format belongs to a real document parser."""
        return fmt is Format.CSV

    def available(self) -> Availability:
        """Always available: the stdlib is the whole dependency."""
        return Availability(ok=True)

    def extract(self, data: bytes, fmt: Format) -> Extraction:
        """Convert CSV bytes to one markdown table.

        Args:
            data: The CSV bytes (UTF-8, BOM tolerated; undecodable bytes
                replaced — a CSV is text or it is nothing).
            fmt: Must be :attr:`Format.CSV`.

        Returns:
            The extraction; CSVs embed nothing, so assets are always empty.

        Raises:
            ProviderInputError: Not CSV work, the file has no rows, or the
                reader refused it (a field past the size bound, a stray
                newline in an unquoted field).
        """
        if not self.supports(fmt):
            raise ProviderInputError(f"csv-builtin extracts CSV only, got {fmt.value!r}")
        text = data.decode("utf-8-sig", errors="replace")
        rows = _rows(text)
        if not rows:
            raise ProviderInputError("CSV has no rows")
        width = max(len(row) for row in rows)
        header, *body = [_pad(row, width) for row in rows]
        lines = [_table_row(header), _table_row(["---"] * width)]
        lines.extend(_table_row(row) for row in body)
        return Extraction(markdown="\n".join(lines) + "\n")


def _rows(text: str) -> list[list[str]]:
    """Every non-empty row, or a stated bad input — csv.Error never escapes.

    The reader raises during ITERATION, not construction, so the whole
    comprehension sits inside the guard.
    """
    previous = csv.field_size_limit(_MAX_FIELD_CHARS)
    try:
        return [row for row in csv.reader(io.StringIO(text), dialect=_dialect(text)) if row]
    except csv.Error as e:
        raise ProviderInputError(f"CSV could not be read: {e}") from e
    finally:
        # The limit is process-global state; leaving it raised would
        # change how every later reader in this run behaves.
        csv.field_size_limit(previous)


def _dialect(text: str) -> type[csv.Dialect] | csv.Dialect:
    """Sniff the delimiter from a leading sample; excel (comma) is the fallback."""
    try:
        return csv.Sniffer().sniff(text[:_SNIFF_SAMPLE_CHARS], delimiters=_FALLBACK_DELIMITERS)
    except csv.Error:
        return csv.excel


def _pad(row: list[str], width: int) -> list[str]:
    return [*row, *[""] * (width - len(row))]


def _table_row(cells: list[str]) -> str:
    escaped = (" ".join(cell.split()).replace("|", "\\|") for cell in cells)
    return "| " + " | ".join(escaped) + " |"
