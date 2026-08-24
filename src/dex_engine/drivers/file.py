"""The file driver: local repo files and URL-served binaries, routed by Format.

Three work shapes, one driver: ``file:<repo-path>`` keys (materialized
media captures) read from the instance tree; http(s) URLs (a PDF served
from an arbitrary address, rerouted here by detection's HEAD sniff) fetch
through the transport with classified failures; and github blob URLs, whose
bytes come from the shared authenticated seam in
:mod:`dex_engine.drivers.gh` because the URL itself serves an HTML viewer
page (and, on a private repo, 404s to an unauthenticated fetch) — the same
seam the github driver reads them through, so a blob's ref resolves the one
way for both. Bytes are then byte-signature sniffed —
authoritative over whatever a server claimed — and handed to the first
available mechanical extractor for the format.

No provider for the format → ``waiting`` + ``needs: extract`` with the
registry's stated reason — and so does a provider that reported available
and then failed at call time. A scanned/image-only document → ``waiting`` +
``needs: ocr``. Embedded assets ride the Result for the run layer to write
under the media caps, ledgered ``via: extract-asset`` — this driver, like
every driver, never touches the ledger or the disk outputs.

The reverse of the web driver's mid-fetch discovery lives here too: a URL
that detection routed to file work (a ``.pdf`` path, a lying HEAD) whose
BYTES turn out to be an HTML page signals a redetection back to ``web`` —
the run layer re-routes once per run, and in-run mutual re-detection parks
for judgment. Bytes decide, never the content type alone. Local ``file:``
work never re-detects: a captured HTML file is not a page to fetch.
"""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from dex_engine.capabilities import Capabilities
from dex_engine.pipeline.classify import (
    Classification,
    ProviderUnavailableError,
    ScannedDocumentError,
)
from dex_engine.pipeline.detect import looks_like_html, sniff_format
from dex_engine.pipeline.types import (
    Format,
    Kind,
    Need,
    Redetection,
    Result,
    Status,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical, resolve_repo_path

from .fetch import FetchFailure, fetch_classified
from .gh import BlobRef, Gh, blob_ref, fetch_blob, run_gh
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
        gh: Gh = run_gh,
    ) -> None:
        """Wire the extract registry, the instance root, and the two fetch seams.

        Args:
            capabilities: The resolved capability registries.
            root: The instance root for ``file:`` work; ``None`` is legal
                only for registries that never fetch local files (pattern
                matching, normalize).
            transport: The HTTP seam for URL-served binaries.
            gh: The gh-CLI seam — a github blob URL's bytes come from the
                authenticated contents API, never from the viewer page.
        """
        self._capabilities = capabilities
        self._root = root
        self._transport = transport
        self._gh = gh

    def matches(self, url: str) -> bool:
        """True for local-file work keys; URLs reach this driver by sniff."""
        return url.startswith("file:")

    def canonical(self, url: str) -> str:
        """File keys are already canonical — the repo path IS the identity."""
        if url.startswith("file:"):
            return url
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Read or download the bytes, sniff the format, extract."""
        loaded = self._load(unit)
        if isinstance(loaded, Result):
            return loaded
        data, name = loaded
        if data.startswith(_LFS_POINTER_PREFIX):
            return Result(
                status=Status.MANUAL,
                meta={},
                reason=f"{name or 'file'} is an unsmudged LFS pointer — run `git lfs pull`",
            )
        # Byte signatures are authoritative over whatever the URL's path or
        # a HEAD claimed. A URL-served body with no document signature whose
        # BYTES read as HTML is a web page mislabeled into file work —
        # re-route it rather than feeding HTML to a document extractor.
        # Bytes decide, never the content type alone: a lying text/html
        # header over signature-less bytes (a real CSV) must proceed to
        # extraction under its claimed format. Only for http(s) work: a
        # local captured file is not a page to fetch.
        if _rerouteable(unit.url) and sniff_format(data) is None and looks_like_html(data):
            return Result(status=Status.QUEUED, meta={}, redetect=Redetection(kind=Kind.WEB))
        fmt = sniff_format(data, name=name) or unit.format
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
        ref = blob_ref(unit.url)
        if ref is not None:
            return self._read_blob(ref)
        return self._download(unit.url)

    def _read_blob(self, ref: BlobRef) -> tuple[bytes, str | None] | Result:
        """A repo-committed document's bytes, through the gh seam.

        The plain transport cannot serve these: a blob URL answers with the
        HTML viewer page, and on a private repo the unauthenticated fetch
        404s — which classified live content ``dead``.
        """
        blob = fetch_blob(self._gh, ref)
        if isinstance(blob, Classification):
            return Result(status=blob.status, meta={}, reason=blob.reason)
        return blob.data, blob.path.rsplit("/", 1)[-1] or None

    def _read_local(self, repo_path: str) -> tuple[bytes, str | None] | Result:
        if self._root is None:
            # An engine wiring bug, not a content problem: the run layer's
            # broad except files it as `error` rather than mislabeling work.
            raise RuntimeError("FileDriver needs an instance root for local file work")
        path = resolve_repo_path(self._root, repo_path)
        if path is None:
            return Result(
                status=Status.MANUAL,
                meta={},
                reason=f"{repo_path} points outside the instance root — heal the capture",
            )
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
        outcome = fetch_classified(self._transport, url)
        if isinstance(outcome, FetchFailure):
            failure = outcome.classification
            return Result(status=failure.status, meta={}, reason=failure.reason)
        tail = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or None
        return outcome.body, tail

    def _extract(self, data: bytes, fmt: Format, name: str | None) -> Result:
        extractor = self._capabilities.extractor(fmt)
        if extractor is None:
            # No mechanical provider for THIS format: waiting, with the
            # registry's stated reason — the cognitive floor surfaces it on
            # the report for the session.
            availability = self._capabilities.available(Need.EXTRACT, fmt)
            return Result(
                status=Status.WAITING, meta={}, needs=Need.EXTRACT, reason=availability.reason
            )
        try:
            # ProviderInputError propagates: the run loop maps it → manual.
            extraction = extractor.extract(data, fmt)
        except ScannedDocumentError as e:
            return Result(status=Status.WAITING, meta={}, needs=Need.OCR, reason=str(e))
        except ProviderUnavailableError as e:
            # A provider that reported available() and then failed at call
            # time: the capability is, in truth, not available. The same
            # re-park the transcribe drain gives it — waiting has no
            # escalation clock — never error + a filed issue about the
            # world's weather.
            return Result(status=Status.WAITING, meta={}, needs=Need.EXTRACT, reason=str(e))
        meta: dict[str, str | int | None] = {
            "title": name,
            "format": fmt.value,
            "via": extractor.name,
        }
        return Result(
            status=Status.DONE, meta=meta, body=extraction.markdown, assets=extraction.assets
        )


def _rerouteable(url: str) -> bool:
    """Whether HTML bytes at ``url`` mean "this was never file work".

    Only for plain http(s) fetches. A captured local file is not a page to
    fetch, and a github blob's bytes came from the contents API — HTML
    there is an HTML file someone committed, not a viewer page, and
    re-routing it to ``web`` would fetch that viewer and bounce straight
    back (a loop park).
    """
    return not url.startswith("file:") and blob_ref(url) is None
