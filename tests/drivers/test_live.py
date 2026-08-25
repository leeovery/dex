"""Live API drift checks — opt-in via ``pytest -m live``.

These hit real endpoints with the real transport. CI and the default run
never execute them (``-m "not live"`` in pyproject); they exist to catch
the world moving under the drivers: fxtwitter's response shape, arxiv's
Atom feed, the wayback availability API.
"""

import json

import pytest

from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.transport import urllib_transport
from dex_engine.drivers.x import XDriver
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import body_of, make_unit

pytestmark = pytest.mark.live


class TestFxtwitterShape:
    def test_a_stable_public_post_still_parses(self):
        # x.com/jack/status/20 — "just setting up my twttr", stable since 2006.
        driver = XDriver(transport=urllib_transport)
        result = driver.fetch(make_unit("https://x.com/jack/status/20", Kind.X))
        assert result.status is Status.DONE
        assert "twttr" in body_of(result)
        assert result.meta["author"] is not None


class TestArxivShape:
    def test_the_export_api_still_speaks_atom(self):
        driver = PaperDriver(transport=urllib_transport)
        result = driver.fetch(make_unit("https://arxiv.org/abs/1706.03762", Kind.PAPER))
        assert result.status is Status.DONE
        assert "Attention" in str(result.meta["title"])
        assert "## Abstract" in body_of(result)


class TestWaybackShape:
    def test_the_availability_api_shape_holds(self):
        lookup = "https://archive.org/wayback/available?url=example.com"
        response = urllib_transport(lookup)
        assert response.ok
        payload = json.loads(response.text())
        assert "archived_snapshots" in payload
