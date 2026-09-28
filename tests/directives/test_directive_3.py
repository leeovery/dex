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
        assert "code fences 2 → 0; table rows 2 → 1" in text
        assert "`ledgers`" in text
        assert "  > A closing thought about ledgers." in text

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


class TestTranscripts:
    def post(
        self, field: Field, *, engine: str, via: str = "whisper-api", digest_after: bool = True
    ) -> None:
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
        assert "- The transcript: 🍢🍢🍢" in materials(field.root)

    @pytest.mark.parametrize("engine", ["0.2.1", "0.2.6", "0.3.0"])
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
                reason="superseded — the transcript of its post stands for this video",
            )
        )
        field.commit("run: a video retired")
        found = survey(field.root)
        assert not isinstance(found, str)
        assert found.videos == ((VIDEO_URL, POST_ITEM),)


class TestCheck:
    LOST = "Intro line."

    def test_a_missing_record_is_named(self, field):
        field.healed(self.LOST)
        unmet = check(field.root)
        assert f"`{RECORD}` is missing" in unmet[0]

    def test_a_kept_page_left_as_it_stands_passes(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept — the lost lines were page chrome")
        assert check(field.root) == []

    def test_a_kept_page_that_changed_is_named(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: kept")
        field.write(PAGE, "edited")
        unmet = check(field.root)
        assert any("is recorded kept but has changed" in u for u in unmet)

    def test_a_restore_needs_the_earlier_copy_byte_for_byte_and_its_digest_gone(self, field):
        earlier = field.healed(self.LOST)
        record(field, f"- {PAGE}: restored — the re-read lost the code and the closing thought")
        field.write(PAGE, page(EARLIER) + " ")
        unmet = check(field.root)
        assert any("is not its earlier copy byte for byte" in u and earlier in u for u in unmet)
        field.write(PAGE, page(EARLIER))
        assert check(field.root) == [
            f"`{DIGEST}` is drawn from what this directive replaced: delete it (step 3)"
        ]
        (field.root / DIGEST).unlink()
        assert check(field.root) == []

    def test_a_restore_keeps_a_digest_drawn_from_the_earlier_copy(self, field):
        field.healed(self.LOST, redigest=False)
        record(field, f"- {PAGE}: restored")
        field.write(PAGE, page(EARLIER))
        assert check(field.root) == []
        (field.root / DIGEST).unlink()
        assert any(f"`{DIGEST}` changed" in u for u in check(field.root))

    def test_a_merge_must_differ_from_both_copies(self, field):
        field.healed(self.LOST)
        record(field, f"- {PAGE}: merged — the intro from the re-read, the rest from before")
        (field.root / DIGEST).unlink()
        assert any("is recorded merged but holds one copy whole" in u for u in check(field.root))
        field.write(PAGE, page(EARLIER + "\n\nOne more line only the re-read held."))
        assert check(field.root) == []

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

    def test_a_wiki_page_may_change(self, field):
        field.healed(self.LOST)
        field.write("wiki/ledgers.md", "a page")
        field.commit("run: a wiki page")
        record(field, f"- {PAGE}: kept")
        field.write("wiki/ledgers.md", "a page, corrected")
        assert check(field.root) == []

    def test_a_transcript_that_is_not_speech_must_be_taken_out_with_its_digest(self, field):
        field.healed(EARLIER)
        TestTranscripts().post(field, engine="0.2.2")
        record(field, f"- {POST}: not speech — a line of emoji over a silent clip")
        unmet = check(field.root)
        assert any("still holds its transcript" in u for u in unmet)
        field.write(
            POST, render_enrichment(POST_URL, HEALED, {"enclosure": VIDEO_URL}, "@a — a clip")
        )
        assert check(field.root) == [
            f"`{POST_DIGEST}` is drawn from what this directive replaced: delete it (step 3)"
        ]
        (field.root / POST_DIGEST).unlink()
        assert check(field.root) == []

    def test_a_transcript_kept_as_speech_must_still_be_there(self, field):
        field.healed(EARLIER)
        TestTranscripts().post(field, engine="0.2.2")
        record(field, f"- {POST}: speech")
        assert check(field.root) == []
        field.write(POST, "gone")
        assert any("is recorded speech but holds no transcript" in u for u in check(field.root))

    def test_a_digest_written_while_the_video_was_gone_must_be_deleted(self, field):
        field.healed(EARLIER)
        TestVideos().restore(field, digest_in_between=True)
        assert check(field.root) == [
            f"`{POST_DIGEST}` is drawn from what this directive replaced: delete it (step 3)"
        ]
        (field.root / POST_DIGEST).unlink()
        assert check(field.root) == []


class TestShipped:
    def test_the_engine_ships_it_after_the_first_two(self):
        numbers = [d.number for d in discover()]
        assert numbers[:3] == [1, 2, 3]

    def test_show_prints_the_instructions_then_the_materials(self, field):
        field.healed("Intro line.")
        text = show(Instance(root=field.root), discover(), 3)
        assert text.startswith(f"directive 3: {directive_3.INTENT}")
        assert "## Do no harm" in text
        assert text.split(MATERIALS_BEGIN)[1].lstrip().startswith("Earlier commit:")
