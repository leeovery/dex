"""Tests for drivers/x.py: fxtwitter fetch, thread walk-up, classified failures."""

import json

from dex_engine.drivers.x import HOP_SLEEP, MAX_HOPS, XDriver
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import Content, Kind, Missing, Refused, Unusable
from dex_engine.pipeline.urls import work_hash
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    content_of,
    evidence_of,
    fixture_text,
    html_response,
    json_response,
    make_unit,
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
        assert isinstance(result, Content)
        body = body_of(result)
        assert body.index("@alice") < body.index("@bob") < body.index("@carol")
        assert "Two things every ingestion pipeline gets wrong" in body
        assert "the ledger, not the corpus, is the work queue" in body

    def test_one_entry_one_file_thread_meta(self):
        # The chain is context for the captured post, not new first-class
        # sources: it lands in this one file, and its length is meta.
        result = content_of(driver_for(full_chain()).fetch(make_unit(CAPTURED_URL, Kind.X)))
        assert result.meta["thread_length"] == 3
        assert result.meta["author"] == "Carol Chen (@carol)"

    def test_chain_media_pooled_captured_posts_first(self):
        result = content_of(driver_for(full_chain()).fetch(make_unit(CAPTURED_URL, Kind.X)))
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
        assert isinstance(result, Content)
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
        assert isinstance(result, Content)
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
        assert isinstance(result, Content)
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
        assert isinstance(result, Content)
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
        assert isinstance(result, Content)
        assert ("GET", API + "status/300") in transport.calls
        assert all("i/web" not in url for _method, url in transport.calls)

    def test_username_form_fetches_by_the_bare_status_path_too(self):
        transport = FakeTransport(full_chain())
        result = XDriver(transport=transport, pace=lambda _seconds: None).fetch(
            make_unit(CAPTURED_URL, Kind.X)
        )
        assert isinstance(result, Content)
        assert transport.calls[0] == ("GET", API + "status/300")


class TestQuotes:
    def test_quote_stays_inline_as_a_blockquote(self):
        # Promoting a quote is harvest judgment, so the driver keeps it
        # inline rather than pointing at it.
        url = "https://x.com/dana/status/400"
        responses = {API + "status/400": api_fixture("quoted-400.json")}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        body = body_of(result)
        assert "> Quoting @erik: A 403 is not a 404." in body
        assert "> Record the difference." in body


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
        assert isinstance(result, Content)
        body = body_of(result)
        assert "Blocked is not dead: a field guide to failure classification" in body
        assert "retry-forever bucket" in body
        assert "https://t.co/Zq8kR2mVw1" in body

    def test_the_article_body_is_never_just_the_shortlink(self):
        # The old fallback rendered ~74 characters: attribution plus a t.co.
        assert len(body_of(self.article_result())) > 200

    def test_an_announcements_own_text_never_wins_over_the_article(self):
        # The field defect: fxtwitter expands the t.co, so `text` arrives as
        # a bare x.com/i/article/<id>. Reading it first stored a two-line
        # enrichment file, ledgered done, while the whole article sat in
        # `article.content.blocks[]` in the same response.
        responses = {API + "status/702": api_fixture("article-blocks-702.json")}
        result = driver_for(responses).fetch(make_unit("https://x.com/hana/status/702", Kind.X))
        assert isinstance(result, Content)
        body = body_of(result)
        assert "the smartest AI in your company" in body
        assert "compounding is obvious" in body  # the last block, not just the preview
        assert len(body) > 800  # the article, not the ~90-character announcement

    def test_article_blocks_keep_their_shape(self):
        responses = {API + "status/702": api_fixture("article-blocks-702.json")}
        result = driver_for(responses).fetch(make_unit("https://x.com/hana/status/702", Kind.X))
        body = body_of(result)
        assert "# How to Build a Company Brain That Gets Smarter Every Week" in body
        assert "## Why the knowledge stays trapped" in body
        assert "- Corrections live in private threads" in body
        assert "> A company brain is the difference" in body
        # Ordered items number in sequence, and a blank block ends the list.
        assert "1. Collect every correction the team made this week" in body
        assert "3. Hand that document to every agent on Monday" in body

    def test_an_expanded_link_only_body_is_not_content(self):
        # The guard was t.co-only; fxtwitter hands over the expanded URL, so
        # the shape that must not ledger done is "a bare link", not "a t.co".
        tweet = {
            "id": "703",
            "text": "https://x.com/i/article/1900000000000000703",
            "raw_text": {"text": "https://x.com/i/article/1900000000000000703", "facets": []},
            "created_at": "Fri Aug 21 09:20:00 +0000 2026",
            "author": {"name": "Hana Iqbal", "screen_name": "hana"},
            "replying_to": None,
            "replying_to_status": None,
        }
        responses = {API + "status/703": json_response({"tweet": tweet})}
        result = driver_for(responses).fetch(make_unit("https://x.com/hana/status/703", Kind.X))
        assert isinstance(result, Unusable)

    def test_a_shared_article_url_parks_saying_what_to_share_instead(self):
        # fxtwitter 404s on an article id and it does not resolve to the
        # carrier post, so the park has to be actionable rather than blank.
        result = driver_for({}).fetch(
            make_unit("https://x.com/i/article/1900000000000000702", Kind.X)
        )
        assert isinstance(result, Unusable)
        assert "capture that post instead" in result.evidence

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
        assert "no text or media" in evidence_of(result)
        assert isinstance(result, Unusable)

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
        assert isinstance(result, Content)
        assert "finally up" in body_of(result)


class TestClassifiedFailures:
    def test_deleted_post_is_dead(self):
        responses = {API + "status/300": json_response({}, status=404)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert isinstance(result, Missing)

    def test_login_walled_post_is_a_permanent_refusal_with_evidence(self):
        responses = {API + "status/300": json_response({}, status=401)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert isinstance(result, Refused)
        assert result.permanent
        assert PAYWALL_REASON in result.evidence

    def test_402_is_a_permanent_refusal_x_answers_it(self):
        responses = {API + "status/300": json_response({}, status=402)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert isinstance(result, Refused)
        assert result.permanent

    def test_rate_limit_is_a_transient_refusal(self):
        responses = {API + "status/300": json_response({}, status=429)}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_unparseable_json_is_a_transient_refusal(self):
        responses = {API + "status/300": html_response("<html>challenge</html>")}
        result = driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "unparseable JSON" in result.evidence


class TestEdges:
    def test_post_with_no_text_and_no_media_is_manual_with_reason(self):
        url = "https://x.com/frank/status/500"
        responses = {API + "status/500": api_fixture("no-text-500.json")}
        result = driver_for(responses).fetch(make_unit(url, Kind.X))
        assert isinstance(result, Unusable)
        assert result.rescuable
        assert "no text" in result.evidence

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
        assert isinstance(result, Content)
        body = body_of(result)
        assert "@gina" in body
        assert "(photo post)" in body
        assert result.media == [
            "https://pbs.example.test/media/p600.jpg",
            "https://pbs.example.test/media/p601.jpg",
        ]

    def test_non_status_url_is_unusable(self):
        result = driver_for({}).fetch(make_unit("https://x.com/carol", Kind.X))
        assert isinstance(result, Unusable)
        assert "not a status URL" in result.evidence


class TestVideoPosts:
    """fxtwitter nests videos beside photos; reading photos alone lied."""

    URL = "https://x.com/ines/status/800"
    VIDEO = "https://video.example.test/ext_tw_video/800/vid/1280x720/v800.mp4"

    def video_result(self):
        responses = {API + "status/800": api_fixture("video-800.json")}
        return driver_for(responses).fetch(make_unit(self.URL, Kind.X))

    def test_a_video_only_post_is_done_with_the_video_pooled(self):
        # It parked `manual` on "fxtwitter returned no text or media" — a
        # statement the payload itself contradicts — and dropped the video
        # the media stage would have fetched.
        result = content_of(self.video_result())
        assert result.media == [self.VIDEO]  # Content states no park; the type says so

    def test_the_body_names_the_media_it_actually_holds(self):
        body = body_of(self.video_result())
        assert "@ines" in body
        assert "(video post)" in body
        assert "(photo post)" not in body

    def test_media_is_pooled_once_however_many_lists_repeat_it(self):
        # `all` is fxtwitter's union of `photos` and `videos`, so every
        # media object appears twice in one payload; pooling it twice would
        # spend two of the item's four media slots on one file.
        result = content_of(self.video_result())
        assert result.media.count(self.VIDEO) == 1

    def _mixed_post(self) -> dict:
        photo = {"type": "photo", "url": "https://pbs.example.test/media/p900.jpg"}
        gif = {"type": "gif", "url": "https://video.example.test/tweet_video/g900.mp4"}
        return {
            "id": "900",
            "text": "",
            "created_at": "Sat Aug 22 15:00:00 +0000 2026",
            "author": {"name": "Ines Duarte", "screen_name": "ines"},
            "replying_to": None,
            "replying_to_status": None,
            "media": {"all": [photo, gif], "photos": [photo], "videos": [gif]},
        }

    def test_photos_and_videos_pool_together_in_post_order(self):
        responses = {API + "status/900": json_response({"tweet": self._mixed_post()})}
        result = content_of(
            driver_for(responses).fetch(make_unit("https://x.com/ines/status/900", Kind.X))
        )
        assert result.media == [
            "https://pbs.example.test/media/p900.jpg",
            "https://video.example.test/tweet_video/g900.mp4",
        ]
        assert "(media post)" in body_of(result)  # neither label alone would be true

    def test_a_payload_without_the_union_list_still_pools_both(self):
        # `all` leads, but nothing depends on it: the typed lists carry the
        # same media, and the existing photo fixtures have only those.
        post = self._mixed_post()
        del post["media"]["all"]
        responses = {API + "status/900": json_response({"tweet": post})}
        result = content_of(
            driver_for(responses).fetch(make_unit("https://x.com/ines/status/900", Kind.X))
        )
        assert result.media == [
            "https://pbs.example.test/media/p900.jpg",
            "https://video.example.test/tweet_video/g900.mp4",
        ]

    def test_a_parents_video_joins_the_pool_behind_the_captured_posts(self):
        # Chain media is pooled, captured post's first — media stage cap and
        # all. A parent's video is media like any other.
        parent = json.loads(fixture_text("fxtwitter", "video-800.json"))["tweet"]
        captured = json.loads(fixture_text("fxtwitter", "captured-300.json"))["tweet"]
        captured["replying_to"], captured["replying_to_status"] = "ines", "800"
        responses = {
            API + "status/300": json_response({"tweet": captured}),
            API + "ines/status/800": json_response({"tweet": parent}),
        }
        result = content_of(driver_for(responses).fetch(make_unit(CAPTURED_URL, Kind.X)))
        assert result.media == ["https://pbs.example.test/media/p300.jpg", self.VIDEO]

    def test_a_payload_with_no_media_at_all_still_says_so_honestly(self):
        # The reason survives — it just has to be true when it is said.
        responses = {API + "status/500": api_fixture("no-text-500.json")}
        result = driver_for(responses).fetch(make_unit("https://x.com/frank/status/500", Kind.X))
        assert result == Unusable(evidence="fxtwitter returned no text or media")
