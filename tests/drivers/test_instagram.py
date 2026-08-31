"""Tests for drivers/instagram.py: the og: parse, the probe walk, the honest parks."""

from dex_engine.drivers.instagram import (
    DEFAULT_BASE_URL,
    MAX_MEDIA_PROBES,
    PROBE_SLEEP,
    InstagramDriver,
)
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.run import MEDIA_MAX_FILES_POOLED
from dex_engine.pipeline.types import Content, Kind, Need, Refused, Unusable
from dex_engine.pipeline.urls import work_hash
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    content_of,
    evidence_of,
    fixture_text,
    html_response,
    make_unit,
    needs_of,
)

BASE = "https://proxy.test"
PHOTO_CODE = "BsOGulcndj-"
REEL_CODE = "DHVrPLrIyQ_"
# A restricted post's share yields one of these instead of a shortcode.
SHARE_TOKEN = "DXRrALviA5hPH4K9L8-2lwQGcLoHWMFKOnkw6E0"  # noqa: S105 — a URL segment, not a secret


def post_url(code: str) -> str:
    return f"https://www.instagram.com/p/{code}/"


def probe_url(code: str, index: int) -> str:
    return f"{BASE}/videos/{code}/{index}"


def image_url(code: str, index: int) -> str:
    return f"{BASE}/images/{code}/{index}"


def page(name: str) -> HttpResponse:
    return html_response(fixture_text("instagram", name))


def og_page(
    *,
    name: str = "Ada Lovelace",
    caption: str = "a caption",
    handle: str = "ada",
    posted: str = "March 18, 2025",
    code: str = PHOTO_CODE,
) -> HttpResponse:
    """A constructed instagram.com head in the shape the captured ones take."""
    return html_response(
        "<html><head>\n"
        "<title>Instagram</title>\n"
        f'<link rel="canonical" href="https://www.instagram.com/p/{code}/" />\n'
        f'<meta property="og:site_name" content="Instagram" />\n'
        f'<meta property="og:title" content="{name} on Instagram: &quot;{caption}&quot;" />\n'
        f'<meta property="og:description" content="7 likes, 2 comments - {handle} on '
        f'{posted}: &quot;{caption}&quot;. " />\n'
        "</head><body></body></html>"
    )


def media(content_type: str) -> HttpResponse:
    """A probe's answer after the redirect: the CDN's type over real bytes."""
    return HttpResponse(status=200, content_type=content_type, body=b"\xff\xd8\xff\xe0 bytes")


def past_the_end() -> HttpResponse:
    """The proxy's out-of-range answer: 200 and zero bytes, never a 404."""
    return HttpResponse(status=200, content_type="", body=b"", content_length=0)


def walk(code: str, *types: str) -> dict[str, HttpResponse]:
    """The probe responses for a post holding exactly ``types``, terminator included."""
    responses = {probe_url(code, index): media(t) for index, t in enumerate(types, 1)}
    if len(types) < MAX_MEDIA_PROBES:
        responses[probe_url(code, len(types) + 1)] = past_the_end()
    return responses


class Pacer:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def driver_for(responses: dict, *, pace=None, base_url: str = BASE) -> InstagramDriver:
    return InstagramDriver(
        base_url=base_url,
        transport=FakeTransport(responses),
        pace=pace or (lambda _seconds: None),
    )


def fetch(responses: dict, url: str, **kwargs):
    return driver_for(responses, **kwargs).fetch(make_unit(url, Kind.INSTAGRAM))


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.INSTAGRAM
        assert driver.sleep == 4.0

    def test_matches_the_whole_instagram_host(self):
        # Whole-host: an Instagram URL this driver declined would fall to the
        # web catch-all, which ledgers the JS app shell as content.
        driver = driver_for({})
        assert driver.matches(post_url(PHOTO_CODE))
        assert driver.matches("https://instagram.com/natgeo")
        assert driver.matches("https://m.instagram.com/explore/tags/dogs/")
        assert driver.matches("https://www.instagram.com/stories/natgeo/123/")
        assert not driver.matches("https://instagram.example.test/p/abc/")
        assert not driver.matches("https://example.test/p/abc/")

    def test_every_share_shape_of_a_post_canonicalizes_to_the_code(self):
        # The shortcode IS the identity: the three post namespaces and the
        # username-prefixed spellings are one work unit.
        driver = driver_for({})
        shapes = [
            f"https://www.instagram.com/p/{PHOTO_CODE}/",
            f"https://instagram.com/reel/{PHOTO_CODE}/",
            f"https://m.instagram.com/tv/{PHOTO_CODE}/",
            f"https://www.instagram.com/world_record_egg/p/{PHOTO_CODE}/",
            f"https://www.instagram.com/natgeo/reel/{PHOTO_CODE}/?igsh=share",
        ]
        assert {driver.canonical(shape) for shape in shapes} == {post_url(PHOTO_CODE)}

    def test_a_share_token_is_owned_and_canonicalizes_in_place(self):
        # Offline canonicalization keys the ledger before any network, so a
        # token stays a token here — it resolves at fetch time.
        driver = driver_for({})
        assert driver.canonical(post_url(SHARE_TOKEN)) == post_url(SHARE_TOKEN)

    def test_different_posts_stay_different_work_units(self):
        driver = driver_for({})
        hashes = {
            work_hash(driver.canonical(url))
            for url in (post_url(PHOTO_CODE), post_url(REEL_CODE), post_url(SHARE_TOKEN))
        }
        assert len(hashes) == 3

    def test_non_post_urls_keep_the_generic_canonical(self):
        driver = driver_for({})
        assert driver.canonical("https://www.instagram.com/natgeo/?ref=share") == (
            "https://instagram.com/natgeo"
        )

    def test_the_shipped_default_names_the_proxy(self):
        assert DEFAULT_BASE_URL == "https://uuinstagram.com"


class TestPageParse:
    def test_a_multi_line_caption_survives_whole_entities_decoded(self):
        result = fetch(
            {post_url(REEL_CODE): page("public-reel.html"), **walk(REEL_CODE, "image/jpeg")},
            post_url(REEL_CODE),
        )
        body = body_of(result)
        assert "Meet the National Geographic 33!" in body
        assert "\n\nIn homage to our 33 founders, we're honoring" in body  # &#039; decoded
        assert body.endswith("#NatGeo33")

    def test_a_drifted_og_format_degrades_to_unknown_never_crashes(self):
        # The live check watches instagram.com's og: format; this pins what
        # happens when it drifts anyway: og: tags present but in no shape
        # the parse knows still land as attributed-unknown content, never a
        # crash over a missing og:title.
        drifted = html_response(
            "<html><head><title>Instagram</title>\n"
            '<meta property="og:description" content="Photos!" />\n'
            "</head><body></body></html>"
        )
        result = content_of(
            fetch(
                {post_url(PHOTO_CODE): drifted, **walk(PHOTO_CODE, "image/jpeg")},
                post_url(PHOTO_CODE),
            )
        )
        assert result.meta["author"] == "unknown (@unknown)"
        assert result.body is not None
        assert result.body.startswith("@unknown — undated")

    def test_a_drifted_og_format_with_no_media_is_no_text_or_media(self):
        # The caption fallback must stay empty when the title parse fails:
        # anything else turns a metadata-less page into done content.
        drifted = html_response(
            "<html><head><title>Instagram</title>\n"
            '<meta property="og:description" content="Photos!" />\n'
            "</head><body></body></html>"
        )
        result = fetch(
            {post_url(PHOTO_CODE): drifted, **walk(PHOTO_CODE)},
            post_url(PHOTO_CODE),
        )
        assert evidence_of(result) == "no text or media"

    def test_the_handle_and_date_come_off_the_description_engagement_dropped(self):
        result = content_of(
            fetch(
                {post_url(REEL_CODE): page("public-reel.html"), **walk(REEL_CODE, "image/jpeg")},
                post_url(REEL_CODE),
            )
        )
        assert result.meta["author"] == "National Geographic (@natgeo)"
        assert result.meta["posted"] == "March 18, 2025"
        # Engagement counts are snapshot noise and never recorded.
        assert "11K likes" not in str(result.meta) + body_of(result)

    def test_the_body_is_attributed(self):
        result = fetch(
            {post_url(PHOTO_CODE): page("public-photo.html"), **walk(PHOTO_CODE, "image/jpeg")},
            post_url(PHOTO_CODE),
        )
        # &#x2019; and &#x1f64c; decode; the attribution mirrors the X driver's.
        body = body_of(result)
        assert body.startswith("@world_record_egg — January 4, 2019\n\nLet\u2019s set a world")
        assert "We got this \U0001f64c" in body

    def test_meta_names_the_post_and_the_proxy_that_served_its_media(self):
        result = content_of(
            fetch(
                {post_url(PHOTO_CODE): page("public-photo.html"), **walk(PHOTO_CODE, "image/jpeg")},
                post_url(PHOTO_CODE),
            )
        )
        assert result.meta["shortcode"] == PHOTO_CODE
        assert result.meta["canonical"] == post_url(PHOTO_CODE)
        assert result.meta["media_via"] == "proxy.test"
        assert result.meta["share_token"] is None
        # `via` is the transcript-provenance stamp — claiming it here would
        # tell a later drain this body already holds a transcript.
        assert "via" not in result.meta

    def test_a_share_token_resolves_off_the_canonical_link(self):
        # The token addresses the post; the media endpoints only know the
        # short code, so the resolved code is what the walk probes.
        result = content_of(
            fetch(
                {
                    post_url(SHARE_TOKEN): page("public-photo.html"),
                    **walk(PHOTO_CODE, "image/jpeg"),
                },
                post_url(SHARE_TOKEN),
            )
        )
        assert result.meta["shortcode"] == PHOTO_CODE
        assert result.meta["share_token"] == SHARE_TOKEN
        assert result.media == [image_url(PHOTO_CODE, 1)]

    def test_a_reel_canonical_link_resolves_too(self):
        # The canonical link of a reel names /reel/<code>/, not /p/<code>/.
        result = content_of(
            fetch(
                {post_url(SHARE_TOKEN): page("public-reel.html"), **walk(REEL_CODE, "image/jpeg")},
                post_url(SHARE_TOKEN),
            )
        )
        assert result.meta["shortcode"] == REEL_CODE

    def test_a_canonical_less_page_keeps_the_requested_code(self):
        responses = {
            post_url(PHOTO_CODE): html_response(
                '<html><head><title>Instagram</title><meta property="og:title" '
                'content="Ada on Instagram: &quot;hi&quot;" /></head><body></body></html>'
            ),
            **walk(PHOTO_CODE, "image/jpeg"),
        }
        assert content_of(fetch(responses, post_url(PHOTO_CODE))).meta["shortcode"] == PHOTO_CODE


class TestPrivateAndGarbage:
    def test_the_bare_shell_parks_for_a_screenshot(self):
        result = fetch({post_url(PHOTO_CODE): page("private-shell.html")}, post_url(PHOTO_CODE))
        assert isinstance(result, Unusable)
        assert result.rescuable  # the run layer's manual park: judgment can act
        assert evidence_of(result) == (
            "private or unavailable; screenshot it and share that if you want it"
        )

    def test_an_og_less_page_without_the_shell_is_blocked_not_private(self):
        # A proxy or CDN error page has no og: tags either, and an
        # unmaintained freebie serves those routinely — condemning one as
        # "private" would be a claim about content nobody checked.
        result = fetch(
            {
                post_url(PHOTO_CODE): html_response(
                    "<html><head><title>502 Bad Gateway</title></head>"
                    "<body><h1>502 Bad Gateway</h1></body></html>"
                )
            },
            post_url(PHOTO_CODE),
        )
        assert isinstance(result, Refused)
        assert not result.permanent  # retried on the blocked lifecycle
        assert "app shell" in evidence_of(result)

    def test_a_page_fetch_failure_flows_through_the_classification(self):
        result = fetch(
            {post_url(PHOTO_CODE): HttpResponse(status=503, content_type="", body=b"")},
            post_url(PHOTO_CODE),
        )
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_no_probe_is_spent_on_a_page_that_did_not_land(self):
        # A park is settled by the page alone — probing a post nobody can
        # see would cost the proxy five requests per parked unit, every run.
        transport = FakeTransport({post_url(PHOTO_CODE): page("private-shell.html")})
        InstagramDriver(base_url=BASE, transport=transport, pace=lambda _s: None).fetch(
            make_unit(post_url(PHOTO_CODE), Kind.INSTAGRAM)
        )
        assert transport.calls == [("GET", post_url(PHOTO_CODE))]


class TestProbeWalk:
    def test_an_all_image_post_keeps_every_image_as_a_proxy_url(self):
        # Proxy URLs, never the signed CDN URLs they redirect to: those
        # expire, and a ledger identity must not.
        result = content_of(
            fetch(
                {
                    post_url(PHOTO_CODE): og_page(),
                    **walk(PHOTO_CODE, "image/jpeg", "image/webp"),
                },
                post_url(PHOTO_CODE),
            )
        )
        assert result.media == [image_url(PHOTO_CODE, 1), image_url(PHOTO_CODE, 2)]

    def test_a_video_post_parks_for_transcription_with_the_enclosure(self):
        result = needs_of(
            fetch(
                {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "video/mp4")},
                post_url(PHOTO_CODE),
            )
        )
        assert result.need is Need.TRANSCRIBE
        # The enclosure key is what the acquisition reads back out of the
        # park file's frontmatter — and the condition for writing it at all.
        assert result.meta["enclosure"] == probe_url(PHOTO_CODE, 1)
        assert result.reason == "post resolved — video awaits transcription"
        assert body_of(result).endswith("a caption")

    def test_video_wins_a_mixed_carousel_and_the_dropped_stills_are_counted(self):
        result = needs_of(
            fetch(
                {
                    post_url(PHOTO_CODE): og_page(),
                    **walk(PHOTO_CODE, "image/jpeg", "video/mp4", "image/jpeg"),
                },
                post_url(PHOTO_CODE),
            )
        )
        assert result.meta["enclosure"] == probe_url(PHOTO_CODE, 2)
        assert result.meta["images_dropped"] == 2

    def test_an_all_image_post_records_no_dropped_images(self):
        result = content_of(
            fetch(
                {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "image/jpeg")},
                post_url(PHOTO_CODE),
            )
        )
        assert "images_dropped" not in result.meta

    def test_the_walk_stops_at_the_zero_byte_answer(self):
        # Out-of-range is a 200, never a 404 — only the body can terminate
        # the walk, and an unregistered probe would be a loud test failure.
        transport = FakeTransport(
            {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "image/jpeg", "image/jpeg")}
        )
        InstagramDriver(base_url=BASE, transport=transport, pace=lambda _s: None).fetch(
            make_unit(post_url(PHOTO_CODE), Kind.INSTAGRAM)
        )
        probes = [url for _method, url in transport.calls if "/videos/" in url]
        assert probes == [probe_url(PHOTO_CODE, index) for index in (1, 2, 3)]

    def test_the_walk_truncates_at_the_cap(self):
        transport = FakeTransport(
            {
                post_url(PHOTO_CODE): og_page(),
                **walk(PHOTO_CODE, *(["image/jpeg"] * (MAX_MEDIA_PROBES + 1))),
            }
        )
        result = content_of(
            InstagramDriver(base_url=BASE, transport=transport, pace=lambda _s: None).fetch(
                make_unit(post_url(PHOTO_CODE), Kind.INSTAGRAM)
            )
        )
        assert len(result.media) == MAX_MEDIA_PROBES
        probes = [url for _method, url in transport.calls if "/videos/" in url]
        assert len(probes) == MAX_MEDIA_PROBES

    def test_the_walk_covers_the_pooled_media_cap(self):
        # The bound is the run layer's pooled cap plus one — one extra probe
        # so a post carrying more media than the pipeline will fetch shows
        # the truncation instead of looking complete. Pinned here because
        # the driver cannot import it: the run layer imports the drivers.
        assert MAX_MEDIA_PROBES == MEDIA_MAX_FILES_POOLED + 1

    def test_each_probe_reads_one_byte_of_body(self):
        responses = {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "video/mp4")}
        transport = FakeTransport(responses)
        InstagramDriver(base_url=BASE, transport=transport, pace=lambda _s: None).fetch(
            make_unit(post_url(PHOTO_CODE), Kind.INSTAGRAM)
        )
        assert transport.limits == [None, 0, 0]  # the page unbounded, the probes at one byte

    def test_the_probes_are_paced_a_second_apart(self):
        pacer = Pacer()
        fetch(
            {
                post_url(PHOTO_CODE): og_page(),
                **walk(PHOTO_CODE, "image/jpeg", "image/jpeg"),
            },
            post_url(PHOTO_CODE),
            pace=pacer,
        )
        # Between three probes, never before the first.
        assert pacer.waits == [PROBE_SLEEP, PROBE_SLEEP]

    def test_a_failed_probe_refuses_the_whole_unit(self):
        # The caption is cheap to refetch on the retry; a carousel silently
        # stored minus the images the walk never reached is the loss no
        # later stage can see.
        result = fetch(
            {
                post_url(PHOTO_CODE): og_page(),
                probe_url(PHOTO_CODE, 1): media("image/jpeg"),
                probe_url(PHOTO_CODE, 2): ConnectionError("connection reset"),
            },
            post_url(PHOTO_CODE),
        )
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "proxy.test" in evidence_of(result)
        assert "instagram_base_url" in evidence_of(result)

    def test_a_probe_answering_with_a_non_media_body_refuses_too(self):
        result = fetch(
            {
                post_url(PHOTO_CODE): og_page(),
                probe_url(PHOTO_CODE, 1): html_response("<html>rate limited</html>"),
            },
            post_url(PHOTO_CODE),
        )
        assert isinstance(result, Refused)
        assert "text/html" in evidence_of(result)

    def test_a_probe_http_failure_names_the_wire_fact(self):
        result = fetch(
            {
                post_url(PHOTO_CODE): og_page(),
                probe_url(PHOTO_CODE, 1): HttpResponse(status=429, content_type="", body=b""),
            },
            post_url(PHOTO_CODE),
        )
        assert "HTTP 429" in evidence_of(result)

    def test_the_configured_host_is_the_one_probed(self):
        result = content_of(
            fetch(
                {
                    post_url(PHOTO_CODE): og_page(),
                    "https://mirror.test/videos/BsOGulcndj-/1": media("image/jpeg"),
                    "https://mirror.test/videos/BsOGulcndj-/2": past_the_end(),
                },
                post_url(PHOTO_CODE),
                base_url="https://mirror.test/",  # a trailing slash must not double up
            )
        )
        assert result.media == ["https://mirror.test/images/BsOGulcndj-/1"]
        assert result.meta["media_via"] == "mirror.test"


class TestEmptyAndNonPost:
    def test_an_empty_caption_with_media_is_done_with_a_minimal_body(self):
        result = content_of(
            fetch(
                {
                    post_url(PHOTO_CODE): og_page(caption="", handle="ada"),
                    **walk(PHOTO_CODE, "image/jpeg"),
                },
                post_url(PHOTO_CODE),
            )
        )
        assert body_of(result) == "@ada — March 18, 2025\n\n(image post)"
        assert result.media == [image_url(PHOTO_CODE, 1)]

    def test_a_caption_less_video_parks_with_no_body_to_write_yet(self):
        result = needs_of(
            fetch(
                {post_url(PHOTO_CODE): og_page(caption=""), **walk(PHOTO_CODE, "video/mp4")},
                post_url(PHOTO_CODE),
            )
        )
        assert result.body is None
        assert result.meta["enclosure"] == probe_url(PHOTO_CODE, 1)

    def test_no_text_and_no_media_is_said_only_when_it_is_true(self):
        result = fetch(
            {post_url(PHOTO_CODE): og_page(caption=""), **walk(PHOTO_CODE)},
            post_url(PHOTO_CODE),
        )
        assert isinstance(result, Unusable)
        assert result.rescuable
        assert evidence_of(result) == "no text or media"

    def test_a_story_parks_for_a_screenshot(self):
        result = fetch({}, "https://www.instagram.com/stories/natgeo/3612345678901234567/")
        assert isinstance(result, Unusable)
        assert result.rescuable
        assert "login-walled" in evidence_of(result)
        assert "screenshot" in evidence_of(result)

    def test_a_profile_root_is_closed_out_naming_the_shape(self):
        result = fetch({}, "https://www.instagram.com/natgeo/")
        assert isinstance(result, Unusable)
        assert not result.rescuable  # nothing there for judgment to rescue
        assert evidence_of(result) == (
            "not an Instagram post — this URL addresses the profile @natgeo"
        )

    def test_an_explore_page_is_closed_out_too(self):
        result = fetch({}, "https://www.instagram.com/explore/tags/dogs/")
        assert isinstance(result, Unusable)
        assert not result.rescuable
        assert "/explore/tags/dogs" in evidence_of(result)

    def test_the_host_root_is_closed_out(self):
        result = fetch({}, "https://www.instagram.com/")
        assert isinstance(result, Unusable)
        assert not result.rescuable

    def test_no_page_is_fetched_for_a_non_post_url(self):
        transport = FakeTransport({})
        driver = InstagramDriver(base_url=BASE, transport=transport, pace=lambda _s: None)
        driver.fetch(make_unit("https://www.instagram.com/natgeo/", Kind.INSTAGRAM))
        assert transport.calls == []


class TestOutcomeShapes:
    def test_an_image_post_is_content_and_a_video_post_is_a_park(self):
        images = fetch(
            {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "image/jpeg")},
            post_url(PHOTO_CODE),
        )
        video = fetch(
            {post_url(PHOTO_CODE): og_page(), **walk(PHOTO_CODE, "video/mp4")},
            post_url(PHOTO_CODE),
        )
        assert isinstance(images, Content)
        assert not isinstance(video, Content)  # a capability park carries no media
