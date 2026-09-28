"""Tests for directive 3: what 0.2.2's re-reads and transcripts left, judged against git history."""

import datetime
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from dex_engine import numbered_log
from dex_engine.directive import MATERIALS_BEGIN, show
from dex_engine.directives import directive_3, discover
from dex_engine.directives.directive_3 import RECORD, check, materials, survey
from dex_engine.pipeline import ledger
from dex_engine.pipeline.enrichment import post_body, render_enrichment
from dex_engine.pipeline.types import Instance, Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

ITEM = "2024-02-10-a-page-worth-keeping-1a2b3c"
URL = "https://example.test/article"
UNIT = work_hash(URL)
PAGE = f"enrichment/{ITEM}/web-{UNIT[:6]}.md"
DIGEST = f"state/digests/{ITEM}.md"

POST_ITEM = "2026-09-20-a-silent-clip-4d5e6f"
POST_URL = "https://x.com/i/status/900"
POST_UNIT = work_hash(POST_URL)
POST = f"enrichment/{POST_ITEM}/x-{POST_UNIT[:6]}.md"
POST_DIGEST = f"state/digests/{POST_ITEM}.md"
VIDEO_URL = "https://video.example.test/900.mp4"
# The reason 0.2.2 closed a video with when its post's transcript landed.
RETIRED = "superseded — the transcript of its post stands for this video"

TABLE = "| a | b |\n|---|---|\n| 1 | 2 |"
CLOSING = "A closing thought about ledgers."
EARLIER = f"Intro line.\n\n```python\nprint('kept')\n```\n\n{TABLE}\n\n{CLOSING}"
LANDED = datetime.date(2024, 2, 10)
HEALED = datetime.date(2026, 9, 27)
_TICK = iter(range(1_000_000))


def entry(url: str, status: Status, *, engine: str = "0.1.17", **fields: object) -> LedgerEntry:
    """A ledger line, each written a second after the one before."""
    fields.setdefault("date", LANDED)
    fields.setdefault("item", ITEM if url == URL else POST_ITEM)
    fields.setdefault("kind", Kind.WEB if url == URL else Kind.X)
    fields.setdefault(
        "at",
        datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=next(_TICK)),
    )
    return LedgerEntry(hash=work_hash(url), url=url, status=status, engine=engine, **fields)  # ty: ignore[invalid-argument-type]


def video(
    url: str,
    status: Status,
    reason: str | None = None,
    *,
    path: str | None = None,
    at: datetime.datetime | None = None,
) -> LedgerEntry:
    """A ledger line for a video of the post's."""
    stamped = {"at": at} if at is not None else {}
    return entry(
        url,
        status,
        job=Job.MEDIA,
        parent=POST_UNIT,
        depth=1,
        reason=reason,
        path=path,
        **stamped,
    )


def page(body: str, *, fetched: datetime.date = LANDED) -> str:
    return render_enrichment(URL, fetched, {"title": "A page worth keeping"}, body)


class Field:
    """An instance written and committed as the field did: landed, synced 0.2.2, healed."""

    def __init__(self, root: Path, git: Callable[..., None]) -> None:
        self.root = root
        self.git = git

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append(self, *entries: LedgerEntry) -> None:
        path = self.root / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(ledger.to_line(e) + "\n" for e in entries)

    def record_migrations(self, *numbers: int, engine: str) -> None:
        for number in numbers:
            numbered_log.append(
                self.root / "state" / "migrations.jsonl", number=number, engine=engine, date=HEALED
            )

    def commit(self, message: str) -> str:
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", message)
        return self.rev("HEAD")

    def rev(self, revision: str) -> str:
        return subprocess.run(  # noqa: S603 — test-built args, no shell
            ["git", "-C", str(self.root), "rev-parse", revision],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

    def landed(self, *, body: str = EARLIER) -> str:
        self.git(self.root, "init", "-q")
        self.write(".gitignore", "cache/\n")
        self.write(
            f"corpus/{ITEM[:4]}/{ITEM}.md", f"---\nid: {ITEM}\nurls:\n  - {URL}\n---\nnote\n"
        )
        self.write(PAGE, page(body))
        self.write(DIGEST, "---\nid: x\n---\n- drawn from the earlier copy\n")
        self.append(entry(URL, Status.DONE, path=PAGE))
        return self.commit("run: ingest")

    def synced(self) -> str:
        self.record_migrations(15, 16, 17, 18, engine="0.2.2")
        self.append(entry(URL, Status.QUEUED, engine="0.2.2", rerun=True, via="migration-18"))
        return self.commit("sync: engine v0.2.2")

    def reread(self, body: str, *, redigest: bool = True) -> str:
        self.write(PAGE, page(body, fetched=HEALED))
        self.append(
            entry(
                URL,
                Status.DONE,
                engine="0.2.2",
                date=HEALED,
                rerun=True,
                via="migration-18",
                path=PAGE,
            )
        )
        if redigest:
            self.write(DIGEST, "---\nid: x\n---\n- drawn from the re-read\n")
        return self.commit("run: rerun wave")

    def healed(self, body: str, *, redigest: bool = True) -> str:
        """Landed, synced and re-read; returns the earlier commit."""
        earlier = self.landed()
        self.synced()
        self.reread(body, redigest=redigest)
        return earlier


@pytest.fixture
def field(tmp_path: Path, own_git: Callable[..., None]) -> Field:
    root = tmp_path / "instance"
    root.mkdir()
    return Field(root, own_git)


def record(field: Field, *lines: str) -> None:
    field.write(RECORD, "\n".join(lines) + "\n")


def why_not(root: Path) -> str:
    found = survey(root)
    assert isinstance(found, str)
    return found


class TestHistory:
    def test_no_repository_has_nothing_to_show(self, tmp_path):
        assert why_not(tmp_path).startswith("this is no git repository")
        assert materials(tmp_path).startswith("History cannot answer here: this is no git")
        assert check(tmp_path) == []

    def test_an_instance_the_heal_migrations_never_reached_has_nothing_to_do(self, field):
        field.landed()
        assert survey(field.root) == "0.2.2's migrations never ran here"
        assert check(field.root) == []

    def test_the_materials_head_each_list_as_the_instructions_name_it(self, field):
        field.landed()
        field.synced()
        text = materials(field.root)
        (shipped,) = [d for d in discover() if d.number == 3]
        lists = (
            "Pages a re-read replaced",
            "Re-reads that lost nothing",
            "Transcripts on x posts",
            "Digests written while a video was gone",
            "Files a re-read removed",
            "Videos no history holds",
        )
        at = [text.index(f"## {name} (0)\n\nNothing.\n") for name in lists]
        assert at == sorted(at)
        assert all(f"**{name}**" in shipped.instructions for name in lists)

    def test_re_reads_cancelled_before_any_landed_leave_nothing_asking_for_work(self, field):
        # An instance that went from 0.2.1 straight to a release that ran the
        # heal migrations and cancelled their re-reads in one sync.
        field.landed()
        field.synced()
        assert "Nothing below asks for work: go to step 5." in materials(field.root)
        field.reread("Intro line.")
        assert "asks for work" not in materials(field.root)

    def test_the_earlier_commit_is_the_one_before_migration_15_was_recorded(self, field):
        earlier = field.healed("Intro line.\n\nA closing thought about ledgers.")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.earlier == earlier

    def test_a_shallow_clone_that_lost_the_earlier_commit_cannot_answer(
        self, field, tmp_path, own_git
    ):
        field.healed("Intro line.")
        shallow = tmp_path / "shallow"
        own_git(tmp_path, "clone", "-q", "--depth", "2", f"file://{field.root}", str(shallow))
        assert why_not(shallow).startswith("this clone is shallow")


class TestPages:
    def test_a_reread_that_lost_prose_code_and_a_row_is_listed_for_judging(self, field):
        field.healed("Intro line.\n\n| a | b |\n|---|---|")
        found = survey(field.root)
        assert not isinstance(found, str)
        (page_,) = found.pages
        assert (page_.path, page_.earlier_path, page_.item) == (PAGE, PAGE, ITEM)
        assert page_.fences == (2, 0)
        assert page_.rows == (2, 1)  # the dashes row holds no content
        assert page_.lost["ledgers"] == 1
        assert page_.digest_since
        text = materials(field.root)
        assert "## Pages a re-read replaced (1)" in text
        assert f"- Re-read by migration 18: {URL}" in text
        assert f"- Earlier path: `{PAGE}`" in text
        assert "code fences 2 → 0; table rows 2 → 1" in text
        assert "- Digest: written after the earlier commit" in text
        assert "`ledgers`" in text
        assert "  > A closing thought about ledgers." in text

    def test_the_lost_words_are_capped_with_a_count_of_the_rest(self, field):
        words = " ".join(f"word{n}" for n in range(30))
        field.landed(body=f"{words}\n\nIntro line.")
        field.synced()
        field.reread("Intro line.")
        text = materials(field.root)
        assert "`word0`" in text
        assert ", and 6 more" in text
        assert text.count("`word") == 24

    def test_a_reread_that_only_gained_is_listed_as_losing_nothing(self, field):
        field.healed(EARLIER + "\n\nA new section the site added.")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.pages == ()
        assert [p.path for p in found.unharmed] == [PAGE]
        assert f"- `{PAGE}`: gained 6 words" in materials(field.root)

    def test_a_reread_that_changed_only_its_fetch_stamp_is_not_listed(self, field):
        field.healed(EARLIER)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert (found.pages, found.unharmed) == ((), ())

    def test_fewer_code_fences_with_every_word_kept_is_a_loss(self, field):
        field.healed(f"Intro line.\n\nprint('kept')\n\n{TABLE}\n\n{CLOSING}")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert [p.fences for p in found.pages] == [(2, 0)]

    def test_a_unit_that_first_landed_through_the_heal_has_no_earlier_copy(self, field):
        field.landed()
        field.synced()
        other = "https://example.test/parked"
        rel = f"enrichment/{ITEM}/web-{work_hash(other)[:6]}.md"
        field.write(rel, render_enrichment(other, HEALED, {}, "First words."))
        field.append(
            entry(other, Status.DONE, engine="0.2.2", rerun=True, via="migration-18", path=rel)
        )
        field.commit("run: a thin park lands")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert (found.pages, found.unharmed) == ((), ())

    def test_a_digest_left_alone_since_the_earlier_commit_is_said_to_be(self, field):
        field.healed("Intro line.", redigest=False)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.pages[0].digest_since is False
        assert "- Digest: unchanged since the earlier commit" in materials(field.root)

    def test_only_lines_the_copy_now_lacks_are_quoted(self, field):
        # The closing line moved up and the intro was reworded: the moved
        # line stands in the copy now, so only the reworded one is gone.
        field.healed(f"{CLOSING}\n\nIntro, reworded.\n\n```python\nprint('kept')\n```\n\n{TABLE}")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.pages[0].lost_lines == ("Intro line.",)
        assert found.pages[0].gained_lines == ("Intro, reworded.",)
        text = materials(field.root)
        assert "- Lines of the copy now the earlier copy lacked (1):\n  > Intro, reworded." in text

    def test_a_rerun_no_heal_migration_asked_for_is_not_listed(self, field):
        # The owner's own re-read of a page is no damage of 0.2.2's, whatever it lost.
        field.landed()
        field.synced()
        field.write(PAGE, page("Intro line.", fetched=HEALED))
        field.append(entry(URL, Status.DONE, engine="0.2.2", date=HEALED, rerun=True, path=PAGE))
        field.commit("run: the owner's re-read")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert (found.pages, found.unharmed) == ((), ())

    def test_a_page_gone_from_the_tree_is_not_listed(self, field):
        field.healed("Intro line.")
        (field.root / PAGE).unlink()
        field.commit("run: the item's files went")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.pages == ()

    def test_a_ledger_line_that_does_not_parse_hides_nothing_after_it(self, field):
        field.landed()
        with (field.root / "state" / "enrichment-ledger.jsonl").open("a") as handle:
            handle.write("{not a ledger line\n")
        field.synced()
        field.reread("Intro line.")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert [page_.path for page_ in found.pages] == [PAGE]

    def test_a_post_whose_text_is_as_it_was_owes_nothing_here(self, field):
        # Its transcript is the transcript list's to judge.
        field.landed()
        field.write(POST, render_enrichment(POST_URL, LANDED, {"via": "fxtwitter"}, "@a — a clip"))
        field.append(entry(POST_URL, Status.DONE, path=POST))
        field.commit("run: the post, before 0.2.2")
        field.synced()
        fields: dict[str, str | int | None] = {"via": "whisper-api", "model": "whisper-1"}
        field.write(
            POST, render_enrichment(POST_URL, HEALED, fields, post_body("@a — a clip", "🍢🍢"))
        )
        field.append(
            entry(
                POST_URL,
                Status.DONE,
                engine="0.2.2",
                date=HEALED,
                rerun=True,
                via="migration-16",
                path=POST,
            )
        )
        field.commit("run: the post heard")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert (found.pages, found.unharmed) == ((), ())
        assert [t.path for t in found.transcripts] == [POST]

    def test_a_file_a_reread_removed_is_listed(self, field):
        field.landed()
        reading = f"enrichment/{ITEM}/media-0.md"
        field.write(reading, "Describes `media-0.png`\n\nA dashboard, read by eye.\n")
        field.commit("run: a reading")
        field.synced()
        (field.root / reading).unlink()
        field.reread("Intro line.")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.removed == ((reading, ITEM),)
        assert f"- `{reading}` (item {ITEM})" in materials(field.root)


class TestTranscripts:
    def post(
        self,
        field: Field,
        *,
        engine: str,
        via: str = "whisper-api",
        digest_before: bool = True,
        digest_after: bool = True,
    ) -> None:
        if digest_before:
            field.write(POST_DIGEST, "---\nid: p\n---\n- before\n")
            field.commit("run: a digest from before")
        fields: dict[str, str | int | None] = {
            "via": via,
            "model": "whisper-1",
            "enclosure": VIDEO_URL,
        }
        field.write(
            POST, render_enrichment(POST_URL, HEALED, fields, post_body("@a — a clip", "🍢🍢🍢"))
        )
        field.append(entry(POST_URL, Status.DONE, engine=engine, date=HEALED, path=POST))
        if digest_after:
            field.write(POST_DIGEST, "---\nid: p\n---\n- after\n")
        field.commit("run: the post lands")

    @pytest.mark.parametrize("engine", ["0.2.2", "0.2.5"])
    def test_a_transcript_an_unasking_engine_wrote_is_listed(self, field, engine):
        field.healed(EARLIER)
        self.post(field, engine=engine)
        found = survey(field.root)
        assert not isinstance(found, str)
        (transcript,) = found.transcripts
        assert (transcript.path, transcript.transcript, transcript.digest_since) == (
            POST,
            "🍢🍢🍢",
            True,
        )
        text = materials(field.root)
        assert f"- Post: {POST_URL}, transcript written by engine {engine}" in text
        assert "- The post: @a — a clip" in text
        assert "- The transcript: 🍢🍢🍢" in text

    def test_posts_passed_over_before_it_hide_nothing(self, field):
        # Each post here sorts before the one holding a transcript, and each
        # is passed over at a different point.
        field.healed(EARLIER)
        passed_over = {
            "https://x.com/i/status/70": ("0.1.17", "@b — an older post"),
            "https://x.com/i/status/81": ("0.2.2", None),
            "https://x.com/i/status/82": ("0.2.2", "@c — a post with no video"),
        }
        for url, (engine, text) in passed_over.items():
            path = f"enrichment/{POST_ITEM}/x-{work_hash(url)[:6]}.md"
            if text is not None:
                field.write(path, render_enrichment(url, HEALED, {"via": "fxtwitter"}, text))
            field.append(entry(url, Status.DONE, engine=engine, date=HEALED, path=path))
        field.commit("run: three posts")
        assert all(work_hash(url) < POST_UNIT for url in passed_over)
        self.post(field, engine="0.2.2")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert [transcript.path for transcript in found.transcripts] == [POST]

    def test_a_transcript_on_anything_but_a_post_is_not_judged_here(self, field):
        # Podcasts and videos were transcribed long before 0.2.2.
        field.healed(EARLIER)
        episode = "https://podcast.example.test/episode-1"
        path = f"enrichment/{ITEM}/podcast-{work_hash(episode)[:6]}.md"
        fields: dict[str, str | int | None] = {"via": "whisper-api", "model": "whisper-1"}
        field.write(path, render_enrichment(episode, HEALED, fields, post_body("notes", "words")))
        field.append(
            entry(
                episode,
                Status.DONE,
                engine="0.2.2",
                date=HEALED,
                item=ITEM,
                kind=Kind.PODCAST,
                path=path,
            )
        )
        field.commit("run: an episode")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts == ()

    def test_a_post_queued_to_be_read_again_is_not_judged(self, field):
        # The next run reads it with an engine that asks for speech first.
        field.healed(EARLIER)
        self.post(field, engine="0.2.2")
        field.append(entry(POST_URL, Status.QUEUED, engine="0.2.7", rerun=True))
        field.commit("run: the post asked for again")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts == ()

    @pytest.mark.parametrize("engine", ["0.2.1", "0.2.6", "0.3.0", "not a version"])
    def test_a_transcript_from_outside_the_unasking_engines_is_not(self, field, engine):
        field.healed(EARLIER)
        self.post(field, engine=engine)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts == ()

    def test_a_post_whose_via_is_its_fetch_holds_no_transcript(self, field):
        field.healed(EARLIER)
        self.post(field, engine="0.2.2", via="fxtwitter")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts == ()

    def test_a_digest_last_written_before_the_transcript_landed_is_said_to_be(self, field):
        field.healed(EARLIER)
        self.post(field, engine="0.2.2", digest_after=False)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts[0].digest_since is False

    def test_a_post_never_digested_has_no_digest_written_after_its_transcript(self, field):
        field.healed(EARLIER)
        self.post(field, engine="0.2.2", digest_before=False, digest_after=False)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.transcripts[0].digest_since is False


class TestVideos:
    def restore(self, field: Field, *, digest_in_between: bool, digest_after: bool = False) -> None:
        video = f"enrichment/{POST_ITEM}/media-0.mp4"
        field.write(POST_DIGEST, "---\nid: p\n---\n- with the video\n")
        field.commit("run: a post and its video")
        if digest_in_between:
            field.write(POST_DIGEST, "---\nid: p\n---\n- the video is gone\n")
            field.commit("run: re-digest while the video is gone")
        field.write(video, "VIDEO")
        field.record_migrations(19, 20, engine="0.2.4")
        field.append(
            entry(
                VIDEO_URL,
                Status.DONE,
                job=Job.MEDIA,
                parent=POST_UNIT,
                depth=1,
                via="migration-20",
                path=video,
            )
        )
        field.commit("sync: engine v0.2.4")
        if digest_after:
            field.write(POST_DIGEST, "---\nid: p\n---\n- the video is back\n")
            field.commit("run: re-digest with the video back")

    def test_a_digest_written_while_the_video_was_gone_is_listed(self, field):
        field.healed(EARLIER)
        self.restore(field, digest_in_between=True)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.gone_digests == (POST_ITEM,)
        assert f"- `{POST_DIGEST}`" in materials(field.root)

    def test_a_digest_written_again_since_the_return_is_not(self, field):
        field.healed(EARLIER)
        self.restore(field, digest_in_between=True, digest_after=True)
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.gone_digests == ()

    def test_a_video_no_history_holds_is_listed(self, field):
        field.healed(EARLIER)
        field.append(
            entry(
                VIDEO_URL,
                Status.SKIPPED,
                job=Job.MEDIA,
                parent=POST_UNIT,
                depth=1,
                reason=RETIRED,
            )
        )
        field.commit("run: a video retired")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.videos == ((VIDEO_URL, POST_ITEM),)

    def test_only_a_video_0_2_2_retired_is_asked_for_again(self, field):
        field.healed(EARLIER)
        field.append(
            video("https://video.example.test/901.mp4", Status.SKIPPED, "set aside by the owner"),
            video(
                "https://video.example.test/902.mp4",
                Status.DONE,
                path=f"enrichment/{POST_ITEM}/media-1.mp4",
            ),
            video(VIDEO_URL, Status.SKIPPED, RETIRED),
        )
        field.commit("run: three videos")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.videos == ((VIDEO_URL, POST_ITEM),)

    def test_a_line_stamped_out_of_order_hides_nothing_after_it(self, field):
        # A clock that jumped back: the second line loses to the first.
        field.healed(EARLIER)
        other = "https://video.example.test/903.mp4"
        late = datetime.datetime(2026, 9, 20, tzinfo=datetime.UTC)
        early = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
        field.append(
            video(other, Status.SKIPPED, "set aside by the owner", at=late),
            video(other, Status.QUEUED, at=early),
            video(VIDEO_URL, Status.SKIPPED, RETIRED),
        )
        field.commit("run: a clock that jumped back")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.videos == ((VIDEO_URL, POST_ITEM),)

    def test_a_digest_written_before_the_heal_is_not_listed_and_hides_nothing(self, field):
        # The page's digest was last written at the earlier commit, before
        # 0.2.2 took any video; it sorts before the post, whose digest was
        # written while the post's video was gone.
        field.landed()
        field.synced()
        field.reread(EARLIER, redigest=False)
        field.write(POST_DIGEST, "---\nid: p\n---\n- the video is gone\n")
        field.commit("run: re-digest while the video is gone")
        page_video = "https://video.example.test/page.mp4"
        for url, parent, item in (
            (page_video, UNIT, ITEM),
            (VIDEO_URL, POST_UNIT, POST_ITEM),
        ):
            path = f"enrichment/{item}/media-0.mp4"
            field.write(path, "VIDEO")
            field.append(
                entry(
                    url,
                    Status.DONE,
                    job=Job.MEDIA,
                    parent=parent,
                    depth=1,
                    item=item,
                    via="migration-20",
                    path=path,
                )
            )
        field.record_migrations(19, 20, engine="0.2.4")
        field.commit("sync: engine v0.2.4")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.gone_digests == (POST_ITEM,)


class TestCheck:
    LOST = "Intro line."
    NO_DIGEST_LINE = f"`{RECORD}` has no kept or revised line for `{DIGEST}` (step 4)"

    def test_a_listed_page_with_no_record_line_is_named(self, field):
        field.healed(self.LOST)
        assert check(field.root) == [
            f"`{RECORD}` has no kept, restored or merged line for `{PAGE}` (step 4)"
        ]

    def test_a_kept_page_left_as_it_stands_passes(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept — the lost lines were page chrome")
        assert check(field.root) == []

    def test_a_record_line_may_name_its_path_in_backticks(self, field):
        field.healed(self.LOST)
        record(field, f"- `{PAGE}`: kept")
        assert check(field.root) == []

    def test_a_kept_page_that_changed_is_named(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        field.write(PAGE, "edited")
        unmet = check(field.root)
        assert any("is recorded kept but has changed" in u for u in unmet)

    def test_a_restore_needs_the_earlier_copy_byte_for_byte_and_its_digest_settled(self, field):
        earlier = field.healed(self.LOST)
        record(field, f"- {PAGE}: restored — the re-read lost the code and the closing thought")
        field.write(PAGE, page(EARLIER) + " ")
        unmet = check(field.root)
        assert any("is not its earlier copy byte for byte" in u and earlier in u for u in unmet)
        field.write(PAGE, page(EARLIER))
        assert check(field.root) == [self.NO_DIGEST_LINE]
        record(field, f"- {PAGE}: restored", f"- {DIGEST}: revised — drawn from the earlier copy")
        unchanged = (
            f"`{DIGEST}` is recorded revised but is unchanged: write it with "
            "`bin/dex enrich item digest --file cache/digest.json` (step 3), or record it "
            "kept when the verb wrote it as it stood"
        )
        assert check(field.root) == [unchanged]
        field.write(DIGEST, "---\nid: x\n---\n- drawn from the earlier copy again\n")
        field.write("state/passes.jsonl", '{"item": "x", "stage": "digest"}\n')
        assert check(field.root) == []

    def test_a_digest_kept_must_be_left_as_it_stands(self, field):
        field.healed(self.LOST, redigest=False)
        record(field, f"- {PAGE}: restored", f"- {DIGEST}: kept — drawn from the earlier copy")
        field.write(PAGE, page(EARLIER))
        assert check(field.root) == []
        field.write(DIGEST, "edited by hand")
        assert any(f"`{DIGEST}` is recorded kept but has changed" in u for u in check(field.root))

    def test_no_digest_may_be_deleted(self, field):
        # An unattended session's permissions refuse the deletion, and a
        # digest can hold what nothing else in the item holds.
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        (field.root / DIGEST).unlink()
        deleted = (
            f"`{DIGEST}` was deleted, and this directive deletes nothing: put it back with "
            f"`git checkout HEAD -- {DIGEST}`"
        )
        assert check(field.root) == [deleted]

    def test_a_revised_digest_nothing_named_is_still_held_to_its_line(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept", f"- {DIGEST}: revised — a fact corrected")
        field.write(DIGEST, "---\nid: x\n---\n- corrected\n")
        assert check(field.root) == []

    SECTION = f"# From the copy saved on {LANDED}"
    NOW = "Intro line.\n\nA line only the re-read found."

    def merged(self, field: Field, *lines: str) -> None:
        field.write(PAGE, page(self.NOW + "\n\n" + "\n\n".join(lines), fetched=HEALED))

    def test_a_merge_is_the_copy_now_with_the_earlier_copys_lines_under_one_heading(self, field):
        field.healed(self.NOW)
        record(field, f"- {PAGE}: merged — the code and the closing thought", f"- {DIGEST}: kept")
        assert any(f"holds no one `{self.SECTION}` section" in u for u in check(field.root))
        self.merged(field, self.SECTION, "```python\nprint('kept')\n```", TABLE, CLOSING)
        assert check(field.root) == []

    def test_the_materials_give_each_page_its_merge_heading(self, field):
        field.healed(self.NOW)
        assert f"- Merge heading: `{self.SECTION}`" in materials(field.root)

    def test_a_merged_line_must_be_the_earlier_copys_word_for_word(self, field):
        field.healed(self.NOW)
        record(field, f"- {PAGE}: merged", f"- {DIGEST}: kept")
        self.merged(field, self.SECTION, "A closing thought about ledgers, paraphrased.")
        assert any(
            "holds a line the earlier copy does not, word for word: "
            "`A closing thought about ledgers, paraphrased.`" in u
            for u in check(field.root)
        )

    def test_a_stray_line_is_quoted_whatever_it_holds(self, field):
        field.healed(self.NOW)
        record(field, f"- {PAGE}: merged", f"- {DIGEST}: kept")
        self.merged(field, self.SECTION, "def f(): return {'a': 1}")
        assert any("`def f(): return {'a': 1}`" in u for u in check(field.root))

    def test_a_merge_leaves_the_copy_now_as_it_stands(self, field):
        field.healed(self.NOW)
        record(field, f"- {PAGE}: merged", f"- {DIGEST}: kept")
        field.write(PAGE, page(f"Intro line.\n\n{self.SECTION}\n\n{CLOSING}", fetched=HEALED))
        assert any("its copy now has changed" in u for u in check(field.root))

    def test_a_merge_that_adds_nothing_is_named(self, field):
        field.healed(self.NOW)
        record(field, f"- {PAGE}: merged", f"- {DIGEST}: kept")
        self.merged(field, self.SECTION, "")
        assert any("section adds nothing" in u for u in check(field.root))

    def test_a_post_kept_whose_transcript_came_out_is_judged_on_its_page_alone(self, field):
        # A post listed as a page and as a transcript: taking the transcript
        # out rewrites the file, and its model and via lines, but not its page.
        self.lost_post(field)
        record(
            field,
            f"- {POST}: kept — only a counter",
            f"- {POST}: not speech — a line of emoji",
            f"- {POST_DIGEST}: kept — it never used the words",
        )
        field.write(POST, render_enrichment(POST_URL, HEALED, self.TAKEN_OUT, "@a — a clip"))
        assert check(field.root) == []

    # A transcript taken out as step 2 says: `model:` gone, `via:` back to
    # the fetch that stood before the transcript landed.
    TAKEN_OUT: dict[str, str | int | None] = {"enclosure": VIDEO_URL, "via": "fxtwitter"}  # noqa: RUF012 — read only

    def test_a_post_merged_whose_transcript_came_out_keeps_its_page(self, field):
        self.lost_post(field)
        record(
            field,
            f"- {POST}: merged — the second line",
            f"- {POST}: not speech — a line of emoji",
            f"- {POST_DIGEST}: kept — it never used the words",
        )
        body = f"@a — a clip\n\n# From the copy saved on {LANDED}\n\nand a second line"
        field.write(POST, render_enrichment(POST_URL, HEALED, self.TAKEN_OUT, body))
        assert check(field.root) == []

    def test_a_merged_post_keeps_its_transcript_below_the_section(self, field):
        self.lost_post(field)
        record(
            field,
            f"- {POST}: merged — the second line",
            f"- {POST}: speech",
            f"- {POST_DIGEST}: kept — it never used the words",
        )
        section = f"# From the copy saved on {LANDED}\n\nand a second line"
        fields: dict[str, str | int | None] = {
            "via": "whisper-api",
            "model": "whisper-1",
            "enclosure": VIDEO_URL,
        }
        body = post_body(f"@a — a clip\n\n{section}", "🍢🍢🍢")
        field.write(POST, render_enrichment(POST_URL, HEALED, fields, body))
        assert check(field.root) == []
        body = post_body(f"@a — a clip\n\n{section}", "🍢🍢")
        field.write(POST, render_enrichment(POST_URL, HEALED, fields, body))
        assert any("its copy now has changed" in u for u in check(field.root))

    def lost_post(self, field: Field) -> None:
        """A post a heal re-read shortened and gave a transcript an unasking engine wrote."""
        field.landed()
        before = "@a — a clip\n\nand a second line"
        field.write(POST, render_enrichment(POST_URL, LANDED, {"via": "fxtwitter"}, before))
        field.append(entry(POST_URL, Status.DONE, path=POST))
        field.commit("run: the post lands")
        field.synced()
        field.reread(EARLIER, redigest=False)
        fields: dict[str, str | int | None] = {
            "via": "whisper-api",
            "model": "whisper-1",
            "enclosure": VIDEO_URL,
        }
        field.write(
            POST, render_enrichment(POST_URL, HEALED, fields, post_body("@a — a clip", "🍢🍢🍢"))
        )
        field.append(
            entry(
                POST_URL,
                Status.DONE,
                engine="0.2.2",
                date=HEALED,
                rerun=True,
                via="migration-17",
                path=POST,
            )
        )
        field.write(POST_DIGEST, "---\nid: p\n---\n- after\n")
        field.commit("run: the post re-read")

    def test_two_outcomes_for_one_page_are_named(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept", f"- {PAGE}: restored")
        assert any("as both kept and restored" in u for u in check(field.root))

    def test_a_change_the_directive_does_not_name_is_named(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        field.write(f"corpus/{ITEM[:4]}/{ITEM}.md", "rewritten")
        field.write("stray.md", "new")
        unmet = check(field.root)
        assert any(f"`corpus/{ITEM[:4]}/{ITEM}.md` changed" in u for u in unmet)
        assert any("`stray.md` is new" in u for u in unmet)

    def test_every_change_it_does_not_name_is_named_whatever_comes_before_it(self, field):
        # In the tree's order: an allowed restore, a deleted digest, a changed
        # ledger, the digest verb's log, then two new files.
        field.healed(self.LOST)
        record(field, f"- {PAGE}: restored", f"- {DIGEST}: kept")
        field.write(PAGE, page(EARLIER))
        (field.root / DIGEST).unlink()
        field.append(entry(URL, Status.DONE, path=PAGE))
        field.write("state/passes.jsonl", '{"item": "x", "stage": "digest"}\n')
        field.write("stray-a.md", "new")
        field.write("stray-b.md", "new")
        unmet = check(field.root)
        for named in (
            f"`{DIGEST}` was deleted",
            "`state/enrichment-ledger.jsonl` changed",
            "`stray-a.md` is new",
            "`stray-b.md` is new",
        ):
            assert any(named in u for u in unmet), named

    def test_a_renamed_file_is_named_once_by_its_new_path(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        field.git(field.root, "mv", f"corpus/{ITEM[:4]}/{ITEM}.md", "corpus/moved.md")
        unmet = check(field.root)
        assert len(unmet) == 1
        assert unmet[0].startswith("`corpus/moved.md` changed")

    def test_a_file_changed_then_deleted_is_named_deleted(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        corpus = f"corpus/{ITEM[:4]}/{ITEM}.md"
        field.write(corpus, "rewritten")
        field.git(field.root, "add", corpus)
        (field.root / corpus).unlink()
        assert any(f"`{corpus}` was deleted" in u for u in check(field.root))

    def test_a_record_may_open_with_a_title(self, field):
        field.healed(self.LOST)
        record(field, "# Directive 3", "", f"- {PAGE}: kept — the lost lines were page chrome")
        assert check(field.root) == []

    def test_an_unreadable_record_is_named(self, field):
        field.healed(self.LOST)
        (field.root / RECORD).parent.mkdir(parents=True, exist_ok=True)
        (field.root / RECORD).write_bytes(b"- \xff\xfe: kept\n")
        assert (
            f"`{RECORD}` is unreadable (UnicodeDecodeError): write it again as step 4 says"
        ) in check(field.root)

    def test_a_wiki_page_may_change(self, field):
        field.healed(self.LOST)
        field.write("wiki/ledgers.md", "a page")
        field.commit("run: a wiki page")
        record(field, f"- {PAGE}: kept")
        field.write("wiki/ledgers.md", "a page, corrected")
        assert check(field.root) == []

    def test_a_transcript_that_is_not_speech_must_be_taken_out_and_its_digest_settled(self, field):
        field.healed(EARLIER)
        TestTranscripts().post(field, engine="0.2.2")
        record(field, f"- {POST}: not speech — a line of emoji over a silent clip")
        unmet = check(field.root)
        assert any("still holds its transcript" in u for u in unmet)
        field.write(
            POST, render_enrichment(POST_URL, HEALED, {"enclosure": VIDEO_URL}, "@a — a clip")
        )
        assert check(field.root) == [
            f"`{RECORD}` has no kept or revised line for `{POST_DIGEST}` (step 4)"
        ]
        record(field, f"- {POST}: not speech", f"- {POST_DIGEST}: kept — it never used the words")
        assert check(field.root) == []

    def test_a_transcript_kept_as_speech_must_still_be_there(self, field):
        field.healed(EARLIER)
        TestTranscripts().post(field, engine="0.2.2")
        record(field, f"- {POST}: speech")
        assert check(field.root) == []
        field.write(POST, "gone")
        assert any("is recorded speech but holds no transcript" in u for u in check(field.root))

    def test_a_digest_written_while_the_video_was_gone_must_be_settled(self, field):
        field.healed(EARLIER)
        TestVideos().restore(field, digest_in_between=True)
        assert check(field.root) == [
            f"`{RECORD}` has no kept or revised line for `{POST_DIGEST}` (step 4)"
        ]
        record(field, f"- {POST_DIGEST}: revised — the video is back")
        field.write(POST_DIGEST, "---\nid: p\n---\n- the video is back\n")
        assert check(field.root) == []


class TestShipped:
    def test_the_engine_ships_it_after_the_first_two(self):
        numbers = [d.number for d in discover()]
        assert numbers[:3] == [1, 2, 3]

    def test_it_permits_the_digest_verb_alone(self):
        (shipped,) = [d for d in discover() if d.number == 3]
        assert shipped.permits == frozenset({"item digest"})

    def test_show_prints_the_instructions_then_the_materials(self, field):
        field.healed("Intro line.")
        text = show(Instance(root=field.root), discover(), 3)
        assert text.startswith(f"directive 3: {directive_3.INTENT}")
        assert "## Do no harm" in text
        assert text.split(MATERIALS_BEGIN)[1].lstrip().startswith("Earlier commit:")
