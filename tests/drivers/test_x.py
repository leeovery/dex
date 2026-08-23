"""Tests for drivers/x.py: fxtwitter fetch, thread walk-up, classified failures."""

import json

from dex_engine.drivers.x import HOP_SLEEP, MAX_HOPS, XDriver
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import Kind, Status
from dex_engine.pipeline.urls import work_hash
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
    return XDriver(transport=FakeTransport(responses), pace=lambda _seconds: None)


def full_chain() -> dict:
    return {
        API + "status/300": api_fixture("captured-300.json"),
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
        assert driver.matches("https://mobile.twitter.com/a/status/1")
        assert not driver.matches("https://example.test/a")

    def test_every_share_shape_of_a_post_canonicalizes_to_the_id(self):
        # The id IS the identity: username form, the app's /i/web/ share
        # form, /i/status/, the legacy /statuses/ spelling, host variants,
        # share params, and /photo/1 tails are all the same work unit.
        driver = driver_for({})
        shapes = [
            "https://x.com/carol/status/300",
            "https://x.com/i/web/status/300",
            "https://x.com/i/status/300",
            "https://x.com/carol/status/300?s=20&t=share-token",
            "https://x.com/carol/status/300/photo/1",
            "https://twitter.com/carol/statuses/300",
            "https://mobile.twitter.com/carol/status/300",
        ]
        assert {driver.canonical(shape) for shape in shapes} == {"https://x.com/i/status/300"}

    def test_different_posts_stay_different_work_units(self):
        driver = driver_for({})
        hashes = {
            work_hash(driver.canonical(url))
            for url in (
                "https://x.com/carol/status/300",
                "https://x.com/carol/status/301",
                "https://x.com/i/web/status/302",
            )
        }
        assert len(hashes) == 3

    def test_non_status_urls_keep_the_generic_canonical(self):
        driver = driver_for({})
        assert driver.canonical("https://x.com/carol?ref_src=share") == "https://x.com/carol"


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
        # Learned from a production run: a chain that fetched short was
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

    def test_walk_stops_at_the_hop_bound_and_notes_it_in_meta(self):
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
        responses[API + f"status/{captured}"] = responses[API + f"user{captured}/status/{captured}"]
        url = f"https://x.com/user{captured}/status/{captured}"
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        assert result.status is Status.DONE
        assert result.meta["thread_cap_hit"] == "true"
        assert result.meta["thread_length"] == MAX_HOPS + 1  # the captured post + the bound

    def _looping_post(self, status_id: str, parent_id: str) -> dict:
        return {
            "id": status_id,
            "text": f"post {status_id}",
            "created_at": "Thu Aug 20 10:00:00 +0000 2026",
            "author": {"name": f"User {status_id}", "screen_name": f"user{status_id}"},
            "replying_to": f"user{parent_id}",
            "replying_to_status": parent_id,
        }

    def test_a_self_referencing_parent_stops_at_the_first_repeat(self):
        # A post naming itself as its own parent used to walk the full
        # bound: 100 back-to-back requests to a free community API for one
        # unit. Seeing the id already walked ends it at zero further calls.
        transport = FakeTransport(
            {API + "status/500": json_response({"tweet": self._looping_post("500", "500")})}
        )
        result = XDriver(transport=transport, pace=lambda _seconds: None).fetch(
            make_unit("https://x.com/loop/status/500", Kind.X)
        )
        assert result.status is Status.DONE
        assert transport.calls == [("GET", API + "status/500")]  # the captured post, and stop
        assert "thread_cap_hit" not in result.meta
        assert result.meta["chain_incomplete"] == "true"
        assert "loops back" in str(result.meta["chain_note"])

    def test_a_cycle_further_up_the_chain_stops_there(self):
        # A -> B -> A: the repeat is two hops up, not at the captured post.
        transport = FakeTransport(
            {
                API + "status/700": json_response({"tweet": self._looping_post("700", "800")}),
                API + "user800/status/800": json_response(
                    {"tweet": self._looping_post("800", "700")}
                ),
            }
        )
        result = XDriver(transport=transport, pace=lambda _seconds: None).fetch(
            make_unit("https://x.com/user700/status/700", Kind.X)
        )
        assert result.status is Status.DONE
        assert result.meta["thread_length"] == 2  # both real posts kept
        assert len(transport.calls) == 2  # and no third request
        assert result.meta["chain_incomplete"] == "true"

    def test_the_walk_paces_itself_between_hops(self):
        # A thread is one unit, and the driver's 4s politeness is spent
        # between units — without pacing here a 30-post thread is 30
        # unpaced requests to a free API in one burst.
        slept: list[float] = []
        driver = XDriver(transport=FakeTransport(full_chain()), pace=slept.append)
        driver.fetch(make_unit(CAPTURED_URL, Kind.X))
        assert slept == [HOP_SLEEP, HOP_SLEEP]  # one per parent fetched, none for the captured

    def test_a_post_with_no_parent_never_sleeps(self):
        responses = full_chain()
        responses[API + "status/300"] = api_fixture("root-100.json")
        slept: list[float] = []
        XDriver(transport=FakeTransport(responses), pace=slept.append).fetch(
            make_unit(CAPTURED_URL, Kind.X)
        )
        assert slept == []


class TestShareShapeFetches:
    def test_i_web_share_link_fetches_the_live_post(self):
        # The app's standard share form: fxtwitter 404s on /i/web/… — sent
        # verbatim it would mark a live post terminally dead.
        transport = FakeTransport(full_chain())
        driver = XDriver(transport=transport, pace=lambda _seconds: None)
        result = driver.fetch(make_unit("https://x.com/i/web/status/300", Kind.X))
        assert result.status is Status.DONE
        assert ("GET", API + "status/300") in transport.calls
        assert all("i/web" not in url for _method, url in transport.calls)

    def test_username_form_fetches_by_the_bare_status_path_too(self):
        transport = FakeTransport(full_chain())
        result = XDriver(transport=transport, pace=lambda _seconds: None).fetch(
            make_unit(CAPTURED_URL, Kind.X)
        )
        assert result.status is Status.DONE
        assert transport.calls[0] == ("GET", API + "status/300")


class TestQuotes:
    def test_quote_stays_inline_as_a_blockquote(self):
        url = "https://x.com/dana/status/400"
        responses = {API + "status/400": api_fixture("quoted-400.json")}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        body = body_of(result)
        assert "> Quoting @erik: A 403 is not a 404." in body
        assert "> Record the difference." in body
        assert result.children == []  # promoting a quote is harvest judgment


class TestArticles:
    """Long-form articles keep their prose under `article`, not `text`."""

    URL = "https://x.com/hana/status/700"

    def article_result(self):
        responses = {API + "status/700": api_fixture("article-700.json")}
        return driver_for(responses).fetch(make_unit(self.URL, Kind.X))

    def test_article_title_and_preview_become_the_body(self):
        # `text` is empty on these and raw_text holds only the shortlink —
        # falling back to raw_text ledgered them done on a ~74-char URL.
        result = self.article_result()
        assert result.status is Status.DONE
        body = body_of(result)
        assert "Blocked is not dead: a field guide to failure classification" in body
        assert "retry-forever bucket" in body
        assert "https://t.co/Zq8kR2mVw1" in body

    def test_the_article_body_is_never_just_the_shortlink(self):
        # The old fallback rendered ~74 characters: attribution plus a t.co.
        assert len(body_of(self.article_result())) > 200

    def test_a_post_that_is_only_a_shortlink_is_not_content(self):
        tweet = {
            "id": "701",
            "text": "",
            "raw_text": {"text": "https://t.co/Zq8kR2mVw1", "facets": []},
            "created_at": "Fri Aug 21 09:20:00 +0000 2026",
            "author": {"name": "Hana Iqbal", "screen_name": "hana"},
            "replying_to": None,
            "replying_to_status": None,
        }
        responses = {API + "status/701": json_response({"tweet": tweet})}
        result = driver_for(responses).fetch(make_unit("https://x.com/hana/status/701", Kind.X))
        assert result.status is Status.MANUAL
        assert "no text or media" in reason_of(result)

    def test_prose_that_merely_contains_a_shortlink_is_still_content(self):
        tweet = {
            "id": "702",
            "text": "The failure-classification piece is finally up: https://t.co/Zq8kR2mVw1",
            "created_at": "Fri Aug 21 09:25:00 +0000 2026",
            "author": {"name": "Hana Iqbal", "screen_name": "hana"},
            "replying_to": None,
            "replying_to_status": None,
        }
        responses = {API + "status/702": json_response({"tweet": tweet})}
        result = driver_for(responses).fetch(make_unit("https://x.com/hana/status/702", Kind.X))
        assert result.status is Status.DONE
        assert "finally up" in body_of(result)


class TestClassifiedFailures:
    def test_deleted_post_is_dead(self):
        responses = {API + "status/300": json_response({}, status=404)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.DEAD

    def test_login_walled_post_is_manual_with_reason(self):
        responses = {API + "status/300": json_response({}, status=401)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.MANUAL
        assert PAYWALL_REASON in reason_of(result)

    def test_402_is_manual_x_answers_it(self):
        responses = {API + "status/300": json_response({}, status=402)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.MANUAL

    def test_rate_limit_is_blocked(self):
        responses = {API + "status/300": json_response({}, status=429)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.BLOCKED

    def test_unparseable_json_is_blocked(self):
        responses = {API + "status/300": html_response("<html>challenge</html>")}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert result.status is Status.BLOCKED
        assert "unparseable JSON" in reason_of(result)


class TestEdges:
    def test_post_with_no_text_and_no_media_is_manual_with_reason(self):
        url = "https://x.com/frank/status/500"
        responses = {API + "status/500": api_fixture("no-text-500.json")}
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
        responses = {API + "status/600": json_response({"tweet": tweet})}
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
