"""The file driver: local repo files and URL-served binaries, routed by Format (§3).

Two work shapes, one driver: ``file:<repo-path>`` keys (materialized media
captures) read from the instance tree; http(s) URLs (a PDF served from an
arbitrary address, rerouted here by detection's HEAD sniff) fetch through
the transport with classified failures. Bytes are then byte-signature
sniffed — authoritative over whatever a server claimed (§1) — and handed to
the first available mechanical extractor for the format (§6).

No provider for the format → ``waiting`` + ``needs: extract`` with the
registry's stated reason. A scanned/image-only document → ``waiting`` +
``needs: ocr``. Embedded assets ride the Result for the run layer to write
under the §7 media caps, ledgered ``via: extract-asset`` — this driver, like
every driver, never touches the ledger or the disk outputs (§2).
"""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from dex_engine.capabilities import Capabilities
from dex_engine.pipeline.classify import (
    ScannedDocumentError,
    classify_connection,
    classify_http,
)
from dex_engine.pipeline.detect import sniff_format
from dex_engine.pipeline.types import Format, Kind, Need, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical

from .transport import Transport, urllib_transport

__all__ = ["FileDriver"]

_LFS_POINTER_PREFIX = b"version https://git-lfs"


class FileDriver:
    """Extract captured or URL-served binaries via the extract registry."""

    kind: Kind = Kind.FILE
    sleep: float = 1.0

    def __init__(
        self,
        *,
        capabilities: Capabilities,
        root: Path | None = None,
        transport: Transport = urllib_transport,
    ) -> None:
        """Wire the extract registry, the instance root, and the HTTP seam.

        Args:
            capabilities: The resolved capability registries (§6).
            root: The instance root for ``file:`` work; ``None`` is legal
                only for registries that never fetch local files (pattern
                matching, normalize).
            transport: The HTTP seam for URL-served binaries.
        """
        self._capabilities = capabilities
        self._root = root
        self._transport = transport

    def matches(self, url: str) -> bool:
        """True for local-file work keys; URLs reach this driver by sniff."""
        return url.startswith("file:")

    def canonical(self, url: str) -> str:
        """File keys are already canonical — the repo path IS the identity (§5)."""
        if url.startswith("file:"):
            return url
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Read or download the bytes, sniff the format, extract."""
        loaded = self._load(unit)
        if isinstance(loaded, Result):
            return loaded
        data, name = loaded
        fmt = sniff_format(data, name=name) or unit.format
        if data.startswith(_LFS_POINTER_PREFIX):
            return Result(
                status=Status.MANUAL,
                meta={},
                reason=f"{name or 'file'} is an unsmudged LFS pointer — run `git lfs pull`",
            )
        if fmt is None:
            return Result(
                status=Status.MANUAL,
                meta={},
                reason=(
                    f"unrecognized file format ({name or 'unnamed'}) — not one of the "
                    "ten extractable formats"
                ),
            )
        return self._extract(data, fmt, name)

    def _load(self, unit: WorkUnit) -> tuple[bytes, str | None] | Result:
        if unit.url.startswith("file:"):
            return self._read_local(unit.url.removeprefix("file:"))
        return self._download(unit.url)

    def _read_local(self, repo_path: str) -> tuple[bytes, str | None] | Result:
        if self._root is None:
            # An engine wiring bug, not a content problem: the run layer's
            # broad except files it as `error` rather than mislabeling work.
            raise RuntimeError("FileDriver needs an instance root for local file work")
        path = self._root / repo_path
        if not path.is_file():
            return Result(
                status=Status.MANUAL,
                meta={},
                reason=(
                    f"{repo_path} is not in the working tree — pull (and `git lfs pull`) "
                    "or heal the capture"
                ),
            )
        return path.read_bytes(), path.name

    def _download(self, url: str) -> tuple[bytes, str | None] | Result:
        try:
            response = self._transport(url)
        except OSError as e:
            failure = classify_connection(e)
            return Result(status=failure.status, meta={}, reason=failure.reason)
        if not response.ok:
            failure = classify_http(response.status)
            return Result(status=failure.status, meta={}, reason=failure.reason)
        tail = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or None
        return response.body, tail

    def _extract(self, data: bytes, fmt: Format, name: str | None) -> Result:
        extractor = self._capabilities.extractor(fmt)
        if extractor is None:
            # No mechanical provider for THIS format (§6): waiting, with the
            # registry's stated reason — the cognitive floor surfaces it on
            # the report for the session.
            availability = self._capabilities.available(Need.EXTRACT, fmt)
            return Result(
                status=Status.WAITING, meta={}, needs=Need.EXTRACT, reason=availability.reason
            )
        try:
            # ProviderInputError propagates: the run loop maps it → manual (§5).
            extraction = extractor.extract(data, fmt)
        except ScannedDocumentError as e:
            return Result(status=Status.WAITING, meta={}, needs=Need.OCR, reason=str(e))
        meta: dict[str, str | int | None] = {
            "title": name,
            "format": fmt.value,
            "via": extractor.name,
        }
        return Result(
            status=Status.DONE, meta=meta, body=extraction.markdown, assets=extraction.assets
        )
