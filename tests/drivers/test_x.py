"""Tests for drivers/x.py: fxtwitter fetch, thread walk-up, classified failures."""

import json

from dex_engine.drivers.x import MAX_HOPS, XDriver
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    fixture_text,
    html_response,
    json_response,
    make_unit,
    reason_of,
)

API = "https://api.fxtwitter.com/"
CAPTURED_URL = "https://x.com/carol/status/300"


def api_fixture(name: str):
    return json_response(json.loads(fixture_text("fxtwitter", name)))


def driver_for(responses: dict) -> XDriver:
    return XDriver(transport=FakeTransport(responses))


def full_chain() -> dict:
    return {
        API + "carol/status/300": api_fixture("captured-300.json"),
        API + "bob/status/200": api_fixture("parent-200.json"),
        API + "alice/status/100": api_fixture("root-100.json"),
    }


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.X
        assert driver.sleep == 4.0

    def test_matches_x_and_twitter_hosts(self):
        driver = driver_for({})
        assert driver.matches("https://x.com/a/status/1")
        assert driver.matches("https://www.twitter.com/a/status/1")
        assert driver.matches("https://m.twitter.com/a/status/1")
        assert not driver.matches("https://example.test/a")

    def test_canonical_strips_tracking_noise(self):
        driver = driver_for({})
        assert (
            driver.canonical("https://x.com/carol/status/300?s=20&ref_src=share")
            == "https://x.com/carol/status/300?s=20"
        )


class TestThreadWalkUp:
    def test_reading_order_is_root_to_captured_all_authors_attributed(self):
        result = driver_for(full_chain()).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.DONE
        body = body_of(result)
        assert body.index("@alice") < body.index("@bob") < body.index("@carol")
        assert "Two things every ingestion pipeline gets wrong" in body
        assert "the ledger, not the corpus, is the work queue" in body

    def test_one_entry_one_file_thread_meta(self):
        result = driver_for(full_chain()).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.meta["thread_length"] == 3
        assert result.meta["author"] == "Carol Chen (@carol)"
        assert result.children == []  # the chain is context, never children

    def test_chain_media_pooled_captured_posts_first(self):
        result = driver_for(full_chain()).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.media == [
            "https://pbs.example.test/media/p300.jpg",
            "https://pbs.example.test/media/p200.jpg",
        ]

    def test_mid_walk_fetch_failure_records_the_gap(self):
        # Learned 2026-08-20 (dex-marketing): a chain that fetched short was
        # silently presented as complete.
        responses = full_chain()
        responses[API + "alice/status/100"] = json_response({}, status=404)
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.DONE
        assert result.meta["chain_incomplete"] == "true"
        assert "after 2 post(s)" in str(result.meta["chain_note"])
        assert "HTTP 404" in str(result.meta["chain_note"])
        body = body_of(result)
        assert "@alice" not in body
        assert body.index("@bob") < body.index("@carol")

    def test_walk_stops_at_the_20_hop_cap_and_notes_it_in_meta(self):
        base = 1000
        responses = {}
        for i in range(base, base + MAX_HOPS + 5):
            tweet = {
                "id": str(i),
                "text": f"post {i}",
                "created_at": "Thu Aug 20 10:00:00 +0000 2026",
                "author": {"name": f"User {i}", "screen_name": f"user{i}"},
                "replying_to": f"user{i - 1}" if i > base else None,
                "replying_to_status": str(i - 1) if i > base else None,
            }
            responses[API + f"user{i}/status/{i}"] = json_response({"tweet": tweet})
        captured = base + MAX_HOPS + 4
        url = f"https://x.com/user{captured}/status/{captured}"
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        assert result.status is Status.DONE
        assert result.meta["thread_cap_hit"] == "true"
        assert result.meta["thread_length"] == MAX_HOPS + 1  # captured + 20 hops


class TestQuotes:
    def test_quote_stays_inline_as_a_blockquote(self):
        url = "https://x.com/dana/status/400"
        responses = {API + "dana/status/400": api_fixture("quoted-400.json")}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        body = body_of(result)
        assert "> Quoting @erik: A 403 is not a 404." in body
        assert "> Record the difference." in body
        assert result.children == []  # promoting a quote is harvest judgment


class TestClassifiedFailures:
    def test_deleted_post_is_dead(self):
        responses = {API + "carol/status/300": json_response({}, status=404)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.DEAD

    def test_login_walled_post_is_manual_with_reason(self):
        responses = {API + "carol/status/300": json_response({}, status=401)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.MANUAL
        assert PAYWALL_REASON in reason_of(result)

    def test_402_is_manual_x_answers_it(self):
        responses = {API + "carol/status/300": json_response({}, status=402)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.MANUAL

    def test_rate_limit_is_blocked(self):
        responses = {API + "carol/status/300": json_response({}, status=429)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.BLOCKED

    def test_unparseable_json_is_blocked(self):
        responses = {API + "carol/status/300": html_response("<html>challenge</html>")}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.BLOCKED
        assert "unparseable JSON" in reason_of(result)


class TestEdges:
    def test_post_with_no_text_and_no_media_is_manual_with_reason(self):
        url = "https://x.com/frank/status/500"
        responses = {API + "frank/status/500": api_fixture("no-text-500.json")}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        assert result.status is Status.MANUAL
        assert "no text" in reason_of(result)

    def test_photo_only_post_is_done_with_attributed_body_and_media(self):
        url = "https://x.com/gina/status/600"
        tweet = {
            "id": "600",
            "text": "",
            "created_at": "Thu Aug 20 12:00:00 +0000 2026",
            "author": {"name": "Gina Ruiz", "screen_name": "gina"},
            "replying_to": None,
            "replying_to_status": None,
            "media": {
                "photos": [
                    {"type": "photo", "url": "https://pbs.example.test/media/p600.jpg"},
                    {"type": "photo", "url": "https://pbs.example.test/media/p601.jpg"},
                ]
            },
        }
        responses = {API + "gina/status/600": json_response({"tweet": tweet})}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        assert result.status is Status.DONE
        body = body_of(result)
        assert "@gina" in body
        assert "(photo post)" in body
        assert result.media == [
            "https://pbs.example.test/media/p600.jpg",
            "https://pbs.example.test/media/p601.jpg",
        ]

    def test_non_status_url_is_manual(self):
        result = driver_for({}).fetch(make_unit("https://x.com/carol", Kind.X))
        assert result.status is Status.MANUAL
        assert "not a status URL" in reason_of(result)
