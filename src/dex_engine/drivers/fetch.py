"""The fetch-and-classify seam: one GET, one classified failure.

Every HTTP caller in the system paired its transport call with the same
guard — ``except OSError`` to ``classify_connection``, non-2xx to
``classify_http`` — hand-rolled at nine sites across the drivers, the
transcribe drain and the media stage. N copies of one failure contract is
the shape the ``IncompleteRead`` incident exploited: a guard that had to
be right at every copy was wrong at all of them at once, and the fix had
to land at a seam because there was no one call site to fix. This module
is that call site for the pairing itself.

It sits beside :mod:`dex_engine.drivers.transport` with the other shared
seams: like ``gh`` it reaches only for pipeline vocabulary, and nothing
here knows about work units or results.

A failure carries the classification AND the wire fact it was read from
(:class:`FetchFailure`), because the callers split two ways: most route
the classification onward as the unit's outcome, while two — the caption
track whose signed URL makes even a 404 transient, and the wayback
lookup whose miss is only a note on the direct failure — must not
inherit the classified status or its framing, and keep their own plain
spelling of what the wire said (:attr:`FetchFailure.detail`).
"""

from dataclasses import dataclass

from dex_engine.pipeline.classify import Classification, classify_connection, classify_http

from .transport import HttpResponse, Transport

__all__ = ["FetchFailure", "fetch_classified"]


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchFailure:
    """One fetch's classified failure, with the wire fact behind it.

    ``http_status`` is the response status for an HTTP-level failure and
    ``None`` for a connection-level one.
    """

    classification: Classification
    http_status: int | None = None

    @property
    def detail(self) -> str:
        """The failure named plainly: ``HTTP <n>``, or the classified reason.

        For the callers that flatten every fetch failure into their own
        outcome and keep only what the wire said, never the classifier's
        status framing.
        """
        if self.http_status is not None:
            return f"HTTP {self.http_status}"
        return self.classification.reason


def fetch_classified(transport: Transport, url: str) -> HttpResponse | FetchFailure:
    """GET ``url``; a 2xx response returns, everything else classifies.

    Args:
        transport: The HTTP seam.
        url: An absolute http(s) URL.

    Returns:
        The successful response, or the classified failure — connection
        failures through ``classify_connection``, non-2xx statuses through
        ``classify_http``.
    """
    try:
        response = transport(url)
    except OSError as e:
        return FetchFailure(classification=classify_connection(e))
    if response.ok:
        return response
    return FetchFailure(classification=classify_http(response.status), http_status=response.status)
