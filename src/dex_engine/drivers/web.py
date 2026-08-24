"""The web driver: registry catch-all over the shared article-fetch seam.

The whole fetch route — classified fetch, mid-fetch re-detection, links-kept
extraction, thin-extraction parking, wayback rescue — is
:func:`dex_engine.drivers.article.fetch_article`, shared with the paper
driver's non-arxiv hosts. This driver owns what being the catch-all means:
it matches everything, canonicalizes by the generic rules, and reads every
page it is handed as an article.
"""

from dex_engine.pipeline.types import Kind, Outcome, WorkUnit
from dex_engine.pipeline.urls import base_canonical

from .article import HtmlExtract, fetch_article, trafilatura_extract
from .transport import Transport, urllib_transport

__all__ = ["WebDriver"]


class WebDriver:
    """Fetch any web page: the ordered registry's catch-all, always last."""

    kind: Kind = Kind.WEB
    sleep: float = 1.0

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        extract: HtmlExtract = trafilatura_extract,
    ) -> None:
        """Wire the fetch and extraction seams.

        Args:
            transport: The HTTP seam; injected so tests are hermetic.
            extract: html -> markdown; injected for the same reason.
        """
        self._transport = transport
        self._extract = extract

    def matches(self, url: str) -> bool:  # noqa: ARG002 — the catch-all matches everything
        """True always — the catch-all; ordering makes this safe."""
        return True

    def canonical(self, url: str) -> str:
        """The generic canonical form (pipeline/urls.py rules)."""
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Fetch and extract one page, wayback fallback on fetch failure."""
        return fetch_article(self._transport, self._extract, unit.url)
