"""Tests for migration 20: give back from git history what engine 0.2.2 degraded."""

import dataclasses
import datetime
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dex_engine.drivers.transport import HttpResponse
from dex_engine.migrations import migration_20, run_pending
from dex_engine.migrations.migration_20 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.types import Instance, Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport
from tests.pipeline.test_run import make_ctx

TODAY = datetime.date(2026, 9, 26)
NOW = datetime.datetime(2026, 9, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)
WRITTEN = datetime.datetime(2026, 9, 22, 12, 0, 0, tzinfo=datetime.UTC)
LANDED = datetime.date(2026, 9, 22)
RERUN_DAY = datetime.date(2026, 9, 25)
RETIRED = "superseded — the transcript of its post stands for this video"

ITEM = "2026-09-22-2102070701784175080-7e0343"
POST_URL = "https://x.com/i/status/2102070701784175080"
POST_HASH = work_hash(POST_URL)
VIDEO_URL = (
    "https://video.twimg.com/amplify_video/2102070681387274244/vid/avc1/720x1280/"
    "qtRfPw0vIfrNkIot.mp4?tag=29"
)
VIDEO_HASH = work_hash(VIDEO_URL)
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 16
DESCRIPTION = "Describes `media-0.mp4`\n\nA woman in grey loungewear lifts her top.\n"
DIR = f"enrichment/{ITEM}"
POST_FILE = f"{DIR}/x-{POST_HASH[:6]}.md"
VIDEO_FILE = f"{DIR}/media-0.mp4"
PHOTO_URL = "https://pbs.twimg.com/media/PhAbCdEfGh.jpg"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.3")


_TICKS = itertools.count()


def line(url: str, status: Status, *, engine: str = "0.1.17", **fields) -> LedgerEntry:
    """A ledger line, stamped a second after the one built before it."""
    fields.setdefault("at", WRITTEN + datetime.timedelta(seconds=next(_TICKS)))
    fields.setdefault("date", LANDED)
    fields.setdefault("item", ITEM)
    fields.setdefault("kind", Kind.X)
    return LedgerEntry(hash=work_hash(url), url=url, status=status, engine=engine, **fields)


def media(status: Status, *, url: str = VIDEO_URL, **fields) -> LedgerEntry:
    return line(url, status, job=Job.MEDIA, parent=POST_HASH, depth=1, **fields)


def write_ledger(root: Path, *entries: LedgerEntry) -> None:
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(ledger.to_line(entry) + "\n" for entry in entries)


def live(root: Path) -> dict[str, LedgerEntry]:
    # Read as of a month on, so every line a test writes is in the past.
    path = root / "state" / "enrichment-ledger.jsonl"
    return ledger.latest_readable(path, now=lambda: NOW + datetime.timedelta(days=30))


def ledger_text(root: Path) -> str:
    return (root / "state" / "enrichment-ledger.jsonl").read_text(encoding="utf-8")


def write_corpus(root: Path, item: str = ITEM, urls: tuple[str, ...] = (POST_URL,)) -> None:
    path = root / "corpus" / item[:4] / f"{item}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        f"---\nid: {item}\nsource: inbox\nchannel: inbox\nshared_by: owner\n"
        f"date: {item[:10]}\nurls:\n{listed}kinds: [x]\nstatus: enriched\n"
        "enrichment: []\n---\nnote\n",
        encoding="utf-8",
    )


class Repo:
    """An instance in a git repository of its own, written and committed as the field did."""

    def __init__(self, root: Path, git) -> None:
        self.root = root
        self.git = git

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str) -> None:
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", message)

    def git_output(self, *args: str) -> bytes:
        return subprocess.run(  # noqa: S603 — test-built args, no shell
            ["git", "-C", str(self.root), *args],  # noqa: S607 — PATH resolution is the contract
            capture_output=True,
            check=True,
        ).stdout

    def head(self) -> str:
        return self.git_output("rev-parse", "HEAD").decode().strip()


class Field(Repo):
    """dex-health's case: an x post with its video pooled and described, then heard by 0.2.2.

    ``landed`` is the state 0.1.17 left and a commit saved; ``retire`` is
    what the 0.2.2 rerun did on top of it — a transcript onto the post, the
    video and its description deleted, the media unit closed superseded.
    """

    def landed(
        self,
        *,
        descriptions: dict[str, str] | None = None,
        video_committed: bool = True,
        http_shared: bool = False,
    ) -> None:
        self.git(self.root, "init", "-q")
        write_corpus(self.root)
        self.write(POST_FILE, "@DrCamRx — the sleeper build\n")
        described = {"media-0.md": DESCRIPTION} if descriptions is None else descriptions
        for name, text in described.items():
            self.write(f"{DIR}/{name}", text)
        write_ledger(
            self.root,
            line(POST_URL, Status.DONE, path=POST_FILE),
            media(Status.QUEUED),
            media(Status.DONE, path=VIDEO_FILE, http_shared=http_shared),
        )
        if video_committed:
            self.write(VIDEO_FILE, VIDEO_BYTES)
        self.commit("run: ingest the post")
        self.write(VIDEO_FILE, VIDEO_BYTES)

    def retire(self, *, gone: tuple[str, ...] = ("media-0.md",)) -> None:
        self.write(POST_FILE, "@DrCamRx — the sleeper build\n\n## Transcript\n\nWords.\n")
        (self.root / VIDEO_FILE).unlink()
        for name in gone:
            (self.root / DIR / name).unlink()
        rerun = {"engine": "0.2.2", "via": "migration-16", "rerun": True, "date": RERUN_DAY}
        write_ledger(
            self.root,
            line(POST_URL, Status.QUEUED, **rerun),
            line(POST_URL, Status.DONE, path=POST_FILE, **rerun),
            media(Status.SKIPPED, engine="0.2.2", reason=RETIRED, date=RERUN_DAY),
        )

    def retired_and_committed(self, **retire) -> None:
        self.landed()
        self.retire(**retire)
        self.commit("run: rerun at engine v0.2.2")


@pytest.fixture
def field(tmp_path, own_git) -> Field:
    return Field(tmp_path, own_git)


class TestRestoreVideos:
    def test_the_retired_video_and_its_description_come_back_from_history(self, field, migration):
        field.retired_and_committed()
        report = migration.apply(field.root)

        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert (field.root / DIR / "media-0.md").read_text(encoding="utf-8") == DESCRIPTION
        unit = live(field.root)[VIDEO_HASH]
        assert (unit.status, unit.path, unit.reason) == (Status.DONE, VIDEO_FILE, None)
        # The very bytes that landed: the line keeps their landing's day and engine.
        assert (unit.via, unit.rerun, unit.engine, unit.date) == (
            "migration-20",
            False,
            "0.1.17",
            LANDED,
        )
        assert unit.at == NOW
        assert (unit.job, unit.parent, unit.depth, unit.item) == (Job.MEDIA, POST_HASH, 1, ITEM)
        (restored,) = report.actions
        assert restored.startswith(f"{ITEM}: restored {VIDEO_FILE} from ")
        assert f"which 0.2.2 deleted when the transcript of {POST_URL} landed" in restored
        assert "with its description media-0.md beside it" in restored
        assert restored.endswith("; its media unit is done at that path again")
        assert report.skipped == []  # no digest to write again
        assert run_mod.items_owing_descriptions(Instance(root=field.root)) == []

    @pytest.mark.parametrize(
        ("descriptions", "was_gone"),
        [
            ({"media-0.md": DESCRIPTION}, "media-0.mp4 and its description were"),
            (
                {"media-0.md": DESCRIPTION, "media-3.md": f"Describes `{VIDEO_FILE}`\n\nMore.\n"},
                "media-0.mp4 and its descriptions were",
            ),
            # dex-marketing's shape: a fenced description 0.2.2 never deleted.
            ({"media-0.md": "---\nmedia: media-0.mp4\n---\n\nA clip.\n"}, "media-0.mp4 was"),
        ],
    )
    def test_a_digest_drawn_while_the_video_was_gone_is_a_repair(
        self, field, migration, descriptions, was_gone
    ):
        # The session re-digested the item once the transcript landed.
        field.landed(descriptions=descriptions)
        deleted = tuple(n for n, text in descriptions.items() if text.startswith("Describes"))
        field.retire(gone=deleted)
        field.write(f"state/digests/{ITEM}.md", "---\n---\n- a fact from the transcript\n")
        field.commit("run: rerun at engine v0.2.2, the item re-digested")
        report = migration.apply(field.root)
        (repair,) = report.skipped
        assert repair.what == f"state/digests/{ITEM}.md"
        assert repair.why == (
            f"re-digest {ITEM} with {VIDEO_FILE} back — its digest may have been drawn while "
            f"{was_gone} gone; carry none of that digest's facts forward"
        )

    def test_it_reads_the_parent_of_the_commit_that_deleted_the_file(self, field, migration):
        field.retired_and_committed()
        landed_commit = field.git_output("rev-parse", "HEAD~1").decode().strip()
        (restored,) = migration.apply(field.root).actions
        assert f"from {landed_commit[:12]}," in restored

    def test_an_instance_named_by_a_relative_path_restores_the_same(
        self, field, migration, monkeypatch
    ):
        field.retired_and_committed()
        monkeypatch.chdir(field.root.parent)
        report = migration.apply(Path(field.root.name))
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert live(field.root)[VIDEO_HASH].path == VIDEO_FILE
        assert report.skipped == []

    def test_a_retirement_not_yet_committed_restores_from_head(self, field, migration):
        # The description was committed after the video, so HEAD holds both
        # where the commit that wrote the video holds only the video.
        field.landed(descriptions={})
        field.write(f"{DIR}/media-0.md", DESCRIPTION)
        field.commit("run: describe the video")
        field.retire()  # the run wrote, and nothing committed yet
        (restored,) = migration.apply(field.root).actions
        assert f"from {field.head()[:12]}," in restored
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert (field.root / DIR / "media-0.md").read_text(encoding="utf-8") == DESCRIPTION

    def test_only_the_descriptions_0_2_2_deleted_come_back(self, field, migration):
        # 0.2.2 deleted what opened `Describes` on the file's bare name or
        # repo path. Anything else gone now was removed by something else.
        by_path = f"Describes `{VIDEO_FILE}`\n\nA later reading.\n"
        legacy = "# media-0.mp4\n\nA slow pan.\n"
        mentions = f"# The clip\n\nFrom {VIDEO_FILE}: a slow pan.\n"
        another = "Describes `media-4.jpg`\n\nA still from media-0.mp4.\n"
        field.landed(
            descriptions={
                "media-0.md": DESCRIPTION,
                "media-2.md": legacy,
                "media-3.md": by_path,
                "media-5.md": another,
                "media-6.md": mentions,
                "media-10.md": "Describes  `media-0.mp4` (7s)\n",
                "web-abc123.md": f"Describes `{VIDEO_FILE}`\n",
            }
        )
        field.retire(gone=("media-0.md", "media-2.md", "media-3.md", "media-5.md", "media-6.md"))
        (field.root / DIR / "media-10.md").unlink()
        (field.root / DIR / "web-abc123.md").unlink()
        field.commit("run: rerun at engine v0.2.2")
        report = migration.apply(field.root)
        assert (field.root / DIR / "media-0.md").read_text(encoding="utf-8") == DESCRIPTION
        assert (field.root / DIR / "media-3.md").read_text(encoding="utf-8") == by_path
        assert (field.root / DIR / "media-10.md").read_text(encoding="utf-8") == (
            "Describes  `media-0.mp4` (7s)\n"
        )
        for gone in ("media-2.md", "media-5.md", "media-6.md", "web-abc123.md"):
            assert not (field.root / DIR / gone).exists()
        (restored,) = report.actions
        assert "with its descriptions media-0.md, media-10.md, media-3.md beside it" in restored

    def test_a_description_standing_with_other_bytes_is_left_and_named(self, field, migration):
        field.retired_and_committed()
        field.write(f"{DIR}/media-0.md", "Describes `media-0.mp4`\n\nWritten again since.\n")
        (restored,) = migration.apply(field.root).actions
        assert "Written again since." in (field.root / DIR / "media-0.md").read_text()
        assert restored.startswith(f"{ITEM}: restored {VIDEO_FILE} from ")
        assert "(media-0.md not put back: another file stands at that name)" in restored
        assert "beside it" not in restored

    def test_a_description_kept_out_leaves_the_next_one_restored(self, field, migration):
        by_path = f"Describes `{VIDEO_FILE}`\n\nA later reading.\n"
        field.landed(descriptions={"media-0.md": DESCRIPTION, "media-3.md": by_path})
        field.retire(gone=("media-0.md", "media-3.md"))
        field.commit("run: rerun at engine v0.2.2")
        field.write(f"{DIR}/media-0.md", "Describes `media-0.mp4`\n\nWritten again since.\n")
        (restored,) = migration.apply(field.root).actions
        assert (field.root / DIR / "media-3.md").read_text(encoding="utf-8") == by_path
        assert "with its description media-3.md beside it" in restored
        assert "(media-0.md not put back: another file stands at that name)" in restored

    def test_a_description_with_no_closing_newline_comes_back_and_moves(self, field, migration):
        field.landed(descriptions={"media-0.md": "Describes `media-0.mp4`"})
        field.retire()
        field.commit("run: rerun at engine v0.2.2")
        field.write(f"{DIR}/media-0.gif", b"GIF89a")
        migration.apply(field.root)
        assert (field.root / DIR / "media-1.md").read_text(encoding="utf-8") == (
            "Describes `media-1.mp4`"
        )

    def test_a_description_standing_as_it_was_is_its_own(self, field, migration):
        # The field shape: one 0.2.2 never deleted, or an apply interrupted
        # after putting it back.
        field.retired_and_committed(gone=())
        (restored,) = migration.apply(field.root).actions
        assert "with its description media-0.md beside it" in restored
        assert "not put back" not in restored

    def test_a_written_description_recorded_as_the_units_output_comes_back(self, field, migration):
        # The unit's video passed the media ceiling, so a session wrote a
        # description and closed the unit on it; 0.2.2 deleted that
        # markdown as if it were the download.
        written = f"{DIR}/x-{VIDEO_HASH[:6]}.md"
        text = "---\nurl: x\n---\n\nOn screen: a chart.\n"
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        field.write(POST_FILE, "@DrCamRx\n")
        field.write(written, text)
        write_ledger(
            field.root,
            line(POST_URL, Status.DONE, path=POST_FILE),
            media(Status.SKIPPED, reason="media exceeds 10MB ceiling"),
            media(Status.DONE, path=written, title="The clip"),
        )
        field.commit("run: heal the oversize video")
        (field.root / written).unlink()
        write_ledger(field.root, media(Status.SKIPPED, engine="0.2.2", reason=RETIRED))
        field.commit("run: rerun at engine v0.2.2")

        report = migration.apply(field.root)
        assert (field.root / written).read_text(encoding="utf-8") == text
        unit = live(field.root)[VIDEO_HASH]
        assert (unit.status, unit.path, unit.title) == (Status.DONE, written, "The clip")
        assert f"restored {written}" in report.actions[0]

    def test_a_landing_a_compact_dropped_is_read_from_the_ledgers_history(self, field, migration):
        field.retired_and_committed()
        ledger.compact(field.root / "state" / "enrichment-ledger.jsonl", now=lambda: NOW)
        assert VIDEO_FILE not in ledger_text(field.root)  # the only record of the file went
        field.commit("lint: compact")
        report = migration.apply(field.root)
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        restored = live(field.root)[VIDEO_HASH]
        assert (restored.path, restored.date, restored.engine) == (VIDEO_FILE, LANDED, "0.1.17")
        assert report.skipped == []

    def test_landings_compacts_dropped_at_different_times_are_each_found(self, field, migration):
        # Each unit's landing comes from the newest committed ledger that
        # still holds it, however far back that is: the photo's stands only
        # before the video landed, the video's only after the photo's went.
        photo_file = f"{DIR}/media-1.jpg"
        path = field.root / "state" / "enrichment-ledger.jsonl"
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        field.write(POST_FILE, "@DrCamRx\n")
        field.write(photo_file, b"PHOTO-BYTES")
        write_ledger(
            field.root,
            line(POST_URL, Status.DONE, path=POST_FILE),
            media(Status.DONE, url=PHOTO_URL, path=photo_file),
        )
        field.commit("run: the post and its photo land")
        (field.root / photo_file).unlink()
        write_ledger(
            field.root, media(Status.SKIPPED, url=PHOTO_URL, engine="0.2.2", reason=RETIRED)
        )
        ledger.compact(path, now=lambda: NOW)
        field.commit("run: the photo retired; lint: compact")
        field.write(VIDEO_FILE, VIDEO_BYTES)
        field.write(f"{DIR}/media-0.md", DESCRIPTION)
        write_ledger(field.root, media(Status.DONE, path=VIDEO_FILE))
        field.commit("run: the video lands")
        field.retire()
        ledger.compact(path, now=lambda: NOW)
        field.commit("run: the video retired; lint: compact")
        report = migration.apply(field.root)
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert (field.root / photo_file).read_bytes() == b"PHOTO-BYTES"
        assert report.skipped == []

    def test_every_retired_unit_is_restored_whatever_came_before_it(self, field, migration):
        # The photo's file was never committed, and its unit is read first;
        # the video after it still comes back.
        photo_file = f"{DIR}/media-1.jpg"
        field.git(field.root, "init", "-q")
        write_ledger(field.root, media(Status.DONE, url=PHOTO_URL, path=photo_file))
        field.landed()
        write_ledger(
            field.root, media(Status.SKIPPED, url=PHOTO_URL, engine="0.2.2", reason=RETIRED)
        )
        field.retire()
        field.commit("run: rerun at engine v0.2.2")
        report = migration.apply(field.root)
        (skip,) = report.skipped
        assert PHOTO_URL in skip.what
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert live(field.root)[VIDEO_HASH].status is Status.DONE

    def test_a_video_whose_file_is_already_back_is_closed_without_a_second_write(
        self, field, migration
    ):
        # An apply interrupted after its writes, before the line that closes the unit.
        field.retired_and_committed()
        field.write(VIDEO_FILE, VIDEO_BYTES)
        report = migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == VIDEO_FILE
        assert not (field.root / DIR / "media-1.mp4").exists()
        assert report.skipped == []

    def test_an_interrupted_restore_above_a_free_slot_stays_in_its_own(self, field, migration):
        # The video landed in the third slot; the slots below are free, and
        # the interrupted apply's write already stands in its own.
        third = f"{DIR}/media-2.mp4"
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        field.write(POST_FILE, "@DrCamRx\n")
        field.write(third, VIDEO_BYTES)
        write_ledger(
            field.root,
            line(POST_URL, Status.DONE, path=POST_FILE),
            media(Status.DONE, path=third),
        )
        field.commit("run: the video lands in the third slot")
        (field.root / third).unlink()
        write_ledger(field.root, media(Status.SKIPPED, engine="0.2.2", reason=RETIRED))
        field.commit("run: rerun at engine v0.2.2")
        field.write(third, VIDEO_BYTES)
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == third
        assert not (field.root / VIDEO_FILE).exists()

    def test_an_interrupted_relocation_finds_its_file_where_it_left_it(self, field, migration):
        field.retired_and_committed()
        field.write(VIDEO_FILE, b"another download since")
        field.write(f"{DIR}/media-1.mp4", VIDEO_BYTES)  # the interrupted apply's write
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == f"{DIR}/media-1.mp4"
        assert not (field.root / DIR / "media-2.mp4").exists()

    def test_the_line_keeps_the_landings_http_license(self, field, migration):
        field.landed(http_shared=True)
        field.retire()
        field.commit("run: rerun at engine v0.2.2")
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].http_shared

    def test_a_landing_merged_in_below_its_retirement_still_answers(self, field, migration):
        # A union merge can write a unit's older landing below the line that
        # retired it: the landing loses as the live line, and every line
        # after it is still read.
        field.retired_and_committed()
        path = field.root / "state" / "enrichment-ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        landing = next(ln for ln in lines if VIDEO_HASH in ln and '"path"' in ln)
        post = [ln for ln in lines if POST_HASH in ln and VIDEO_HASH not in ln]
        rest = [ln for ln in lines if ln is not landing and ln not in post]
        path.write_text("".join([*rest, landing, *post]), encoding="utf-8")
        field.commit("merge: the landing written below its retirement")
        (restored,) = migration.apply(field.root).actions
        assert f"when the transcript of {POST_URL} landed" in restored
        assert (field.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES

    def test_a_file_standing_in_the_slot_sends_the_restore_to_the_next_free_one(
        self, field, migration
    ):
        # A file no path names is nobody's to take over, and never overwritten.
        field.retired_and_committed()
        field.write(VIDEO_FILE, b"another download since")
        report = migration.apply(field.root)
        assert (field.root / VIDEO_FILE).read_bytes() == b"another download since"
        assert (field.root / DIR / "media-1.mp4").read_bytes() == VIDEO_BYTES
        assert (field.root / DIR / "media-1.md").read_text(encoding="utf-8") == (
            DESCRIPTION.replace("media-0.mp4", "media-1.mp4")
        )
        assert not (field.root / DIR / "media-0.md").exists()
        assert live(field.root)[VIDEO_HASH].path == f"{DIR}/media-1.mp4"
        (restored,) = report.actions
        assert restored.startswith(f"{ITEM}: restored {DIR}/media-1.mp4 from ")
        assert "into slot 1, since another unit's file holds the slot of media-0.mp4" in restored
        assert "with its description media-1.md beside it" in restored

    def test_a_moved_description_has_only_its_name_respelled(self, field, migration):
        # By repo path or bare, the name in the first line becomes the new
        # file's bare name; a description kept under its own name moves too.
        by_path = f"Describes `{VIDEO_FILE}` (7s, `h264`)\n\nOf media-0.mp4: a pan.\n"
        field.landed(descriptions={"media-0.md": DESCRIPTION, "media-3.md": by_path})
        field.retire(gone=("media-0.md", "media-3.md"))
        field.commit("run: rerun at engine v0.2.2")
        field.write(f"{DIR}/media-0.gif", b"GIF89a")
        migration.apply(field.root)
        assert (field.root / DIR / "media-1.md").read_text(encoding="utf-8") == (
            DESCRIPTION.replace("media-0.mp4", "media-1.mp4")
        )
        assert (field.root / DIR / "media-3.md").read_text(encoding="utf-8") == (
            "Describes `media-1.mp4` (7s, `h264`)\n\nOf media-0.mp4: a pan.\n"
        )

    def test_a_slot_a_compact_freed_and_a_download_took_is_left_to_it(self, field, migration):
        # The compact dropped the retired unit's landing, which freed its
        # slot, and a later download took it under another extension. A
        # restore into that slot gave it two owners, and the next
        # re-download's slot sweep deleted the other's file.
        field.retired_and_committed()
        ledger.compact(field.root / "state" / "enrichment-ledger.jsonl", now=lambda: NOW)
        field.write(f"{DIR}/media-0.jpg", b"\xff\xd8\xff PHOTO")
        field.write(f"{DIR}/media-0.md", "Describes `media-0.jpg`\n\nA chart.\n")
        write_ledger(field.root, media(Status.DONE, url=PHOTO_URL, path=f"{DIR}/media-0.jpg"))
        field.commit("lint: compact; run: a photo lands in the freed slot")

        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == f"{DIR}/media-1.mp4"
        assert (field.root / DIR / "media-1.mp4").read_bytes() == VIDEO_BYTES
        assert (
            (field.root / DIR / "media-1.md")
            .read_text(encoding="utf-8")
            .startswith("Describes `media-1.mp4`")
        )
        assert (
            (field.root / DIR / "media-0.md")
            .read_text(encoding="utf-8")
            .startswith("Describes `media-0.jpg`")
        )
        # The photo re-downloads into its own slot, and its sweep takes nothing of ours.
        write_ledger(field.root, media(Status.QUEUED, url=PHOTO_URL))
        transport = FakeTransport(
            {
                PHOTO_URL: HttpResponse(
                    status=200, content_type="image/jpeg", body=b"\xff\xd8\xff NEW"
                )
            }
        )
        later = NOW + datetime.timedelta(hours=1)
        ctx = make_ctx(
            Instance(root=field.root),
            FakeDriver(),
            transport=transport,
            today=lambda: TODAY,
            now=lambda: later,
        )
        run_mod.run(ctx)
        assert (field.root / DIR / "media-0.jpg").read_bytes() == b"\xff\xd8\xff NEW"
        assert (field.root / DIR / "media-1.mp4").read_bytes() == VIDEO_BYTES

    def test_a_slot_another_retired_unit_will_take_back_is_left_to_it(self, field, migration):
        # Two retired units: one relocating must not take the slot the other
        # is about to be restored into.
        photo_file = f"{DIR}/media-1.jpg"
        field.landed()
        field.write(photo_file, b"PHOTO-BYTES")
        write_ledger(field.root, media(Status.DONE, url=PHOTO_URL, path=photo_file))
        field.commit("run: the photo lands")
        (field.root / photo_file).unlink()
        write_ledger(
            field.root, media(Status.SKIPPED, url=PHOTO_URL, engine="0.2.2", reason=RETIRED)
        )
        field.retire()
        field.commit("run: rerun at engine v0.2.2")
        field.write(VIDEO_FILE, b"another download since")
        migration.apply(field.root)
        assert (field.root / photo_file).read_bytes() == b"PHOTO-BYTES"
        assert (field.root / DIR / "media-2.mp4").read_bytes() == VIDEO_BYTES
        assert live(field.root)[VIDEO_HASH].path == f"{DIR}/media-2.mp4"

    def test_a_slot_another_units_path_names_is_left_to_it(self, field, migration):
        # The other unit's file is missing on disk; its recorded path still holds the slot.
        field.retired_and_committed()
        write_ledger(field.root, media(Status.DONE, url=PHOTO_URL, path=f"{DIR}/media-0.jpg"))
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == f"{DIR}/media-1.mp4"

    def test_a_closed_units_old_path_holds_no_slot(self, field, migration):
        field.retired_and_committed()
        write_ledger(
            field.root,
            media(Status.DONE, url=PHOTO_URL, path=f"{DIR}/media-0.jpg"),
            media(Status.DEAD, url=PHOTO_URL),
        )
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == VIDEO_FILE

    def test_another_items_download_in_the_same_slot_is_no_claim(self, field, migration):
        other = "2026-09-20-another-post-000000"
        write_corpus(field.root, item=other, urls=("https://x.com/i/status/1",))
        field.retired_and_committed()
        field.write(f"enrichment/{other}/media-0.jpg", b"PHOTO")
        write_ledger(
            field.root,
            media(Status.DONE, url=PHOTO_URL, item=other, path=f"enrichment/{other}/media-0.jpg"),
        )
        migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].path == VIDEO_FILE

    def test_a_written_output_whose_name_another_file_holds_is_not_restored(self, field, migration):
        # A written description has no slot to move to.
        written = f"{DIR}/x-{VIDEO_HASH[:6]}.md"
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        field.write(written, "On screen: a chart.\n")
        write_ledger(field.root, media(Status.DONE, path=written))
        field.commit("run: heal the oversize video")
        write_ledger(field.root, media(Status.SKIPPED, engine="0.2.2", reason=RETIRED))
        (field.root / written).unlink()
        field.commit("run: rerun at engine v0.2.2")
        field.write(written, "Something else since.\n")
        (skip,) = migration.apply(field.root).skipped
        assert skip.what == f"{ITEM}: {written} of media unit {VIDEO_URL}"
        assert "another file stands at its path now" in skip.why
        assert f"`enrich mark {VIDEO_URL} done --path {written}`" in skip.why
        assert (field.root / written).read_text(encoding="utf-8") == "Something else since.\n"

    def test_a_unit_whose_file_came_back_by_a_later_landing_is_left_alone(self, field, migration):
        field.retired_and_committed()
        field.write(VIDEO_FILE, b"re-fetched")
        write_ledger(field.root, media(Status.DONE, engine="0.2.2", path=VIDEO_FILE))
        before = ledger_text(field.root)
        report = migration.apply(field.root)
        assert (field.root / VIDEO_FILE).read_bytes() == b"re-fetched"
        assert ledger_text(field.root) == before
        assert report.actions == []

    @pytest.mark.parametrize(
        "fields",
        [
            {"status": Status.SKIPPED, "reason": "media exceeds 10MB ceiling"},
            {"status": Status.MANUAL, "reason": RETIRED},
            {"status": Status.SKIPPED, "reason": RETIRED, "job": None},
        ],
    )
    def test_a_unit_closed_any_other_way_is_no_member(self, field, migration, fields):
        field.landed()
        write_ledger(field.root, dataclasses.replace(media(Status.QUEUED), **fields))
        before = ledger_text(field.root)
        report = migration.apply(field.root)
        assert ledger_text(field.root) == before
        assert report.actions == report.skipped == []

    def test_a_second_apply_finds_nothing(self, field, migration):
        field.retired_and_committed()
        migration.apply(field.root)
        before = ledger_text(field.root)
        report = migration.apply(field.root)
        assert ledger_text(field.root) == before
        assert report.actions == report.skipped == []


class TestRenamedItem:
    """A rename moves the item's directory, and a path recorded before it names the dead id."""

    NEW = "2026-09-22-sleeper-build-7e0343"

    def rename(self, field: Field) -> None:
        (field.root / "corpus" / ITEM[:4] / f"{ITEM}.md").unlink()
        write_corpus(field.root, item=self.NEW)
        (field.root / DIR).rename(field.root / "enrichment" / self.NEW)

    def test_after_the_retirement_the_file_comes_back_under_the_live_item(self, field, migration):
        field.retired_and_committed()
        self.rename(field)
        field.commit("item: rename")
        report = migration.apply(field.root)
        folder = field.root / "enrichment" / self.NEW
        assert (folder / "media-0.mp4").read_bytes() == VIDEO_BYTES
        assert (folder / "media-0.md").read_text(encoding="utf-8") == DESCRIPTION
        assert not (field.root / DIR).exists()  # never a directory for the dead id
        unit = live(field.root)[VIDEO_HASH]
        assert (unit.item, unit.path) == (self.NEW, f"enrichment/{self.NEW}/media-0.mp4")
        assert report.skipped == []

    def test_before_the_retirement_the_file_comes_back_from_the_live_directory(
        self, field, migration
    ):
        # Retired where it stood then, under the live item: history holds it
        # at that path, not at the one its landing recorded.
        field.landed()
        self.rename(field)
        field.commit("item: rename")
        folder = field.root / "enrichment" / self.NEW
        revised = DESCRIPTION + "Revised after the rename.\n"
        (folder / "media-0.md").write_text(revised, encoding="utf-8")
        field.commit("run: the description revised")
        (folder / "media-0.mp4").unlink()
        (folder / "media-0.md").unlink()
        write_ledger(field.root, media(Status.SKIPPED, engine="0.2.2", reason=RETIRED))
        field.commit("run: rerun at engine v0.2.2")
        report = migration.apply(field.root)
        assert (folder / "media-0.mp4").read_bytes() == VIDEO_BYTES
        assert (folder / "media-0.md").read_text(encoding="utf-8") == revised
        assert report.skipped == []


class TestHistoryThatCannotAnswer:
    def test_outside_a_repository_nothing_is_restored_and_the_route_is_named(
        self, field, migration
    ):
        field.retired_and_committed()
        shutil.rmtree(field.root / ".git")
        before = ledger_text(field.root)
        report = migration.apply(field.root)
        assert not (field.root / VIDEO_FILE).exists()
        assert ledger_text(field.root) == before
        (skip,) = report.skipped
        assert skip.what == f"{ITEM}: {VIDEO_FILE} of media unit {VIDEO_URL}"
        assert "git answered nothing here" in skip.why
        assert "nothing was restored and the unit still reads skipped" in skip.why
        assert f"requeue it with `enrich mark {VIDEO_URL} queued`" in skip.why

    def test_a_file_never_committed_is_named(self, field, migration):
        field.landed(video_committed=False)
        field.retire()
        report = migration.apply(field.root)
        (skip,) = report.skipped
        assert skip.what == f"{ITEM}: {VIDEO_FILE} of media unit {VIDEO_URL}"
        assert "no commit in history holds it, so it was never committed" in skip.why

    def test_a_unit_no_ledger_line_ever_placed_is_named(self, field, migration):
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        write_ledger(
            field.root,
            line(POST_URL, Status.DONE, path=POST_FILE),
            media(Status.SKIPPED, engine="0.2.2", reason=RETIRED),
        )
        field.write(POST_FILE, "@DrCamRx\n")
        field.commit("run: a ledger that never recorded the video's file")
        report = migration.apply(field.root)
        (skip,) = report.skipped
        assert skip.what == f"{ITEM}: the file of media unit {VIDEO_URL}"
        assert skip.why.startswith(
            "no ledger line, current or committed, records its file; nothing was restored"
        )
        assert f"`enrich mark {VIDEO_URL} queued`" in skip.why

    def test_a_landing_nowhere_in_history_costs_no_walk_through_it(
        self, field, migration, monkeypatch
    ):
        # Only the commits whose ledger change mentions the unit are read,
        # however many others touched the ledger.
        field.git(field.root, "init", "-q")
        write_corpus(field.root)
        field.write(POST_FILE, "@DrCamRx\n")
        write_ledger(field.root, line(POST_URL, Status.DONE, path=POST_FILE))
        field.commit("run: the post lands")
        for n in range(6):
            write_ledger(field.root, line(f"https://example.test/{n}", Status.DONE, path=None))
            field.commit(f"run: another unit {n}")
        write_ledger(field.root, media(Status.SKIPPED, engine="0.2.2", reason=RETIRED))
        field.commit("run: the video retired, its landing never written")
        asked: list[list[str]] = []
        real = migration_20.git_output

        def counted(root, args):
            asked.append(list(args))
            return real(root, args)

        monkeypatch.setattr(migration_20, "git_output", counted)
        (skip,) = migration.apply(field.root).skipped
        assert "no ledger line, current or committed, records its file" in skip.why
        shows = [args for args in asked if args[0] == "show"]
        assert len(shows) == 2  # the commit that closed the unit, and its parent
        assert len(asked) == 5  # the clone's two answers, one pickaxe query, two shows

    def test_a_unit_whose_directory_is_gone_is_named(self, field, migration):
        field.retired_and_committed()
        shutil.rmtree(field.root / DIR)
        report = migration.apply(field.root)
        assert not (field.root / DIR).exists()
        (skip,) = report.skipped
        assert skip.what == f"{ITEM}: {VIDEO_FILE} of media unit {VIDEO_URL}"
        assert skip.why.startswith("its item has no directory; nothing was restored")

    def test_a_unit_no_live_item_claims_is_named(self, field, migration):
        field.retired_and_committed()
        (field.root / "corpus" / ITEM[:4] / f"{ITEM}.md").unlink()
        (skip,) = migration.apply(field.root).skipped
        assert not (field.root / VIDEO_FILE).exists()
        assert skip.why.startswith("no live corpus item claims it; nothing was restored")

    def test_a_file_git_cannot_read_back_is_named(self, field, migration, monkeypatch):
        field.retired_and_committed()
        monkeypatch.setattr(migration_20, "checkout_bytes", lambda *_args: None)
        report = migration.apply(field.root)
        (skip,) = report.skipped
        assert skip.what == f"{ITEM}: {VIDEO_FILE} of media unit {VIDEO_URL}"
        assert "but could not read it; nothing was restored" in skip.why

    def test_a_shallow_clone_says_its_depth_may_hide_the_file(self, field, migration, tmp_path):
        field.retired_and_committed()
        clone = tmp_path / "clone"
        field.git(tmp_path, "clone", "-q", "--depth", "1", f"file://{field.root}", str(clone))
        report = migration.apply(clone)
        assert not (clone / VIDEO_FILE).exists()
        (skip,) = report.skipped
        assert "this clone is shallow" in skip.why

    def test_a_written_description_names_the_route_that_writes_it_again(self, field, migration):
        field.retired_and_committed()
        shutil.rmtree(field.root / ".git")
        written = f"{DIR}/x-{VIDEO_HASH[:6]}.md"
        write_ledger(
            field.root,
            media(Status.DONE, path=written),
            media(Status.SKIPPED, engine="0.2.2", reason=RETIRED),
        )
        (skip,) = migration.apply(field.root).skipped
        assert f"`enrich mark {VIDEO_URL} done --path {written}`" in skip.why


class TestLfs:
    @pytest.fixture
    def lfs(self, field):
        if shutil.which("git-lfs") is None:
            pytest.skip("git-lfs is not installed")
        field.git(field.root, "init", "-q")
        field.git(field.root, "lfs", "install", "--local")
        field.write(
            ".gitattributes", "enrichment/**/media-[0-9]*.* filter=lfs diff=lfs merge=lfs -text\n"
        )
        field.retired_and_committed()
        stored = field.git_output("show", f"HEAD~1:{VIDEO_FILE}")
        assert stored.startswith(b"version https://git-lfs")  # history holds a pointer
        return field

    def test_an_lfs_video_comes_back_as_the_video(self, lfs, migration):
        report = migration.apply(lfs.root)
        assert (lfs.root / VIDEO_FILE).read_bytes() == VIDEO_BYTES
        assert "LFS pointer" not in report.actions[0]

    def test_an_lfs_video_no_store_holds_comes_back_as_its_pointer(self, lfs, migration):
        shutil.rmtree(lfs.root / ".git" / "lfs" / "objects")
        report = migration.apply(lfs.root)
        restored = (lfs.root / VIDEO_FILE).read_bytes()
        assert restored == lfs.git_output("show", f"HEAD~1:{VIDEO_FILE}")
        assert report.actions[0].startswith(f"{ITEM}: restored {VIDEO_FILE} from ")
        assert "as its LFS pointer" in report.actions[0]
        # Staged, the pointer is the very blob history holds.
        lfs.git(lfs.root, "add", VIDEO_FILE)
        staged = lfs.git_output("ls-files", "-s", VIDEO_FILE).split()[1].decode()
        assert staged == lfs.git_output("rev-parse", f"HEAD~1:{VIDEO_FILE}").decode().strip()


# ---------------------------------------------------------------------------
# Pages a 0.2.2 re-read degraded
# ---------------------------------------------------------------------------

PAGE_ITEM = "2026-08-14-docs-page-abc123"
PAGE_URL = "https://docs.example.test/guide/tables"
PAGE_HASH = work_hash(PAGE_URL)
PAGE_FILE = f"enrichment/{PAGE_ITEM}/web-{PAGE_HASH[:6]}.md"
STORED = (
    "---\nurl: https://docs.example.test/guide/tables\nfetched: 2026-08-14\n---\n\n"
    "# Tables\n\n```python\nprint(1)\n```\n\nProse after the code.\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n"
)
FLATTENED = (
    "---\nurl: https://docs.example.test/guide/tables\nfetched: 2026-09-25\n---\n\n"
    "# Tables\n\nprint(1)\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
)
ROWS_LOST = (
    "---\nurl: https://docs.example.test/guide/tables\nfetched: 2026-09-25\n---\n\n"
    "# Tables\n\n```python\nprint(1)\n```\n\nProse after the code.\n\na b 1 2\n"
)
READ_ON = datetime.date(2026, 8, 14)
REREAD_ON = datetime.date(2026, 9, 25)


def page_line(status: Status, *, engine: str, **fields) -> LedgerEntry:
    fields.setdefault("date", READ_ON)
    fields.setdefault("item", PAGE_ITEM)
    fields.setdefault("kind", Kind.WEB)
    return line(PAGE_URL, status, engine=engine, **fields)


class Site(Repo):
    """A page 0.2.1 stored, the sync to 0.2.2 that queued its re-read, and the re-read."""

    def landed(self, body: str = STORED, *, engine: str = "0.1.9") -> None:
        self.git(self.root, "init", "-q")
        write_corpus(self.root, item=PAGE_ITEM, urls=(PAGE_URL,))
        self.write(PAGE_FILE, body)
        write_ledger(
            self.root,
            page_line(Status.DONE, engine=engine, path=PAGE_FILE, title="Tables in the guide"),
        )
        self.commit("run: ingest the page")

    def synced(self, *, commit: bool = True) -> None:
        log = self.root / "state" / "migrations.jsonl"
        with log.open("a", encoding="utf-8") as handle:
            for number in range(1, 19):
                record = {"number": number, "engine": "0.2.2", "date": "2026-09-25"}
                handle.write(json.dumps(record) + "\n")
        seed = {"via": "migration-18", "rerun": True, "date": REREAD_ON}
        write_ledger(self.root, page_line(Status.QUEUED, engine="0.2.2", **seed))
        if commit:
            self.commit("sync: engine v0.2.2")

    def reread(self, body: str = FLATTENED, *, path: str = PAGE_FILE) -> None:
        self.write(path, body)
        write_ledger(self.root, self.reread_line(path))
        self.commit("run: reruns drained")

    def reread_line(self, path: str = PAGE_FILE, **fields) -> LedgerEntry:
        written = {
            "via": "migration-18",
            "rerun": True,
            "date": REREAD_ON,
            "path": path,
            "title": "Tables — the guide",
        }
        return page_line(Status.DONE, engine="0.2.2", **(written | fields))

    def degraded(self, body: str = FLATTENED) -> None:
        self.landed()
        self.synced()
        self.reread(body)


@pytest.fixture
def site(tmp_path, own_git) -> Site:
    return Site(tmp_path, own_git)


class TestRestorePages:
    @pytest.mark.parametrize("body", [FLATTENED, ROWS_LOST], ids=["fences", "rows"])
    def test_a_page_the_reread_degraded_gets_the_sync_commits_copy(self, site, migration, body):
        site.degraded(body)
        sync = site.git_output("rev-parse", "HEAD~1").decode().strip()
        report = migration.apply(site.root)

        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED
        page = live(site.root)[PAGE_HASH]
        assert (page.status, page.path, page.item) == (Status.DONE, PAGE_FILE, PAGE_ITEM)
        # Dated today, the day the enrichment changed back; the engine and
        # title of the copy that stands.
        assert (page.date, page.engine, page.title) == (TODAY, "0.1.9", "Tables in the guide")
        assert (page.via, page.rerun, page.at) == ("migration-20", False, NOW)
        (restored,) = report.actions
        assert restored.startswith(
            f"{PAGE_ITEM}: restored {PAGE_FILE} from the 0.2.2 sync commit {sync[:12]} — "
        )
        assert report.skipped == []  # no digest to write again

    def test_the_report_counts_both_copies(self, site, migration):
        site.degraded()
        (restored,) = migration.apply(site.root).actions
        assert (
            "the re-read held 0 fence line(s) and 3 table row(s) where the copy before it held "
            "2 and 3"
        ) in restored

    def digested(self, site, on: datetime.date) -> Instance:
        instance = Instance(root=site.root)
        instance.digests_dir.mkdir(parents=True)
        (instance.digests_dir / f"{PAGE_ITEM}.md").write_text("digest\n")
        record = {"item": PAGE_ITEM, "stage": "digest", "date": on.isoformat()}
        instance.passes_path.write_text(json.dumps(record) + "\n")
        return instance

    def test_a_digest_drawn_from_the_reread_is_a_repair_for_the_session(self, site, migration):
        site.degraded()
        self.digested(site, REREAD_ON)
        report = migration.apply(site.root)
        (repair,) = report.skipped
        assert repair.what == f"state/digests/{PAGE_ITEM}.md"
        assert repair.why == (
            f"re-digest {PAGE_ITEM} — its digest may have been drawn from the copy the 0.2.2 "
            f"re-read stored — write it again from the restored {PAGE_FILE}; carry none of that "
            "digest's facts forward"
        )
        (restored,) = report.actions
        assert "digest" not in restored

    def test_a_digest_older_than_today_lists_on_the_backstop_too(self, site, migration):
        site.degraded()
        instance = self.digested(site, REREAD_ON)
        assert run_mod.digest_orphans(instance) == []
        migration.apply(site.root)
        assert run_mod.digest_orphans(instance) == [PAGE_ITEM]

    def test_a_digest_written_the_day_of_the_restore_is_still_a_repair(self, site, migration):
        # The backstop compares days, so it cannot see this one: the repair line is the signal.
        site.degraded()
        instance = self.digested(site, TODAY)
        report = migration.apply(site.root)
        assert run_mod.digest_orphans(instance) == []
        assert [repair.what for repair in report.skipped] == [f"state/digests/{PAGE_ITEM}.md"]

    @pytest.mark.parametrize(
        ("stored", "reread", "restored"),
        [
            ("~~~\nx\n~~~\n", "x\n", True),
            ("  ```\n  x\n  ```\n", "x\n", True),
            ("\t```\nx\n```\n", "x\n", True),
            ("  | a |\n", "a\n", True),
            ("text | a |\n", "text a\n", False),
            ("``` x ```\n", "x\n", True),
            ("```\nx\n```\n", "```\nx\n```\n| more |\n", False),
            ("| a |\n", "```\nx\n```\n", True),
        ],
    )
    def test_fence_lines_and_table_rows_decide(self, site, migration, stored, reread, restored):
        site.landed(stored)
        site.synced()
        site.reread(reread)
        report = migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == (
            stored if restored else reread
        )
        assert len(report.actions) == (1 if restored else 0)

    def test_a_reread_that_lost_nothing_is_left_as_it_is(self, site, migration):
        site.degraded(STORED.replace("Prose after", "Different prose after"))
        before = ledger_text(site.root)
        report = migration.apply(site.root)
        assert "Different prose after" in (site.root / PAGE_FILE).read_text(encoding="utf-8")
        assert ledger_text(site.root) == before
        assert report.actions == report.skipped == []

    def test_a_page_a_session_restored_by_hand_is_not_degraded(self, site, migration):
        # git restore of the stored copy, then `mark done` on it: the mark
        # line carries the rerun's provenance and the engine that marked it.
        site.degraded()
        site.write(PAGE_FILE, STORED)
        marked = site.reread_line(title="Tables in the guide")
        write_ledger(site.root, dataclasses.replace(marked, date=TODAY))
        site.commit("run: restore the page the re-read flattened")
        before = ledger_text(site.root)
        report = migration.apply(site.root)
        assert ledger_text(site.root) == before
        assert report.actions == report.skipped == []

    def test_an_item_renamed_after_the_sync_is_read_under_its_old_directory(self, site, migration):
        new = "2026-08-14-tables-guide-abc123"
        site.landed()
        site.synced()
        (site.root / "corpus" / PAGE_ITEM[:4] / f"{PAGE_ITEM}.md").unlink()
        write_corpus(site.root, item=new, urls=(PAGE_URL,))
        (site.root / "enrichment" / PAGE_ITEM).rename(site.root / "enrichment" / new)
        moved = PAGE_FILE.replace(PAGE_ITEM, new)
        site.reread(path=moved)
        report = migration.apply(site.root)
        assert (site.root / moved).read_text(encoding="utf-8") == STORED
        assert not (site.root / PAGE_FILE).exists()
        page = live(site.root)[PAGE_HASH]
        assert (page.item, page.path) == (new, moved)
        assert report.skipped == []

    def test_a_reread_recorded_under_a_dead_id_is_read_where_its_file_stands(self, site, migration):
        # Renamed after the re-read: its line names the old directory.
        new = "2026-08-14-tables-guide-abc123"
        site.degraded()
        (site.root / "corpus" / PAGE_ITEM[:4] / f"{PAGE_ITEM}.md").unlink()
        write_corpus(site.root, item=new, urls=(PAGE_URL,))
        (site.root / "enrichment" / PAGE_ITEM).rename(site.root / "enrichment" / new)
        site.commit("item: rename")
        migration.apply(site.root)
        moved = PAGE_FILE.replace(PAGE_ITEM, new)
        assert (site.root / moved).read_text(encoding="utf-8") == STORED
        assert live(site.root)[PAGE_HASH].path == moved

    def test_the_first_commit_recording_the_sync_is_the_one_read(self, site, migration):
        # A union merge can write the record again later; by then the
        # re-read had landed, and that commit holds the degraded copy.
        site.degraded()
        log = site.root / "state" / "migrations.jsonl"
        record = {"number": 18, "engine": "0.2.2", "date": "2026-09-25"}
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        site.commit("merge: another machine's sync")
        migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED

    def test_a_commit_that_only_took_the_record_away_is_passed_over(self, site, migration):
        site.git(site.root, "init", "-q")
        log = site.root / "state" / "migrations.jsonl"
        site.write("state/migrations.jsonl", '{"number": 18, "engine": "0.2.2"} torn\n')
        site.commit("sync: a torn record")
        log.write_text(
            "".join(
                json.dumps({"number": n, "engine": "0.2.1", "date": "2026-09-01"}) + "\n"
                for n in range(1, 18)
            )
        )
        site.commit("repair: the torn record, the earlier records kept")
        (site.root / "state" / "migrations.jsonl").unlink()
        site.commit("repair: the log goes")
        site.degraded()
        migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED

    def test_a_torn_record_before_the_sync_record_is_passed_over(self, site, migration):
        site.landed()
        site.write("state/migrations.jsonl", '{"number": 3, "engi\n')
        site.synced()
        site.reread()
        migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED

    def test_a_sync_commit_that_begins_the_history_is_read(self, site, migration):
        # A history that starts at the sync, as a fresh import of the instance would.
        site.git(site.root, "init", "-q")
        write_corpus(site.root, item=PAGE_ITEM, urls=(PAGE_URL,))
        site.write(PAGE_FILE, STORED)
        write_ledger(
            site.root,
            page_line(Status.DONE, engine="0.1.9", path=PAGE_FILE, title="Tables in the guide"),
        )
        site.synced()
        site.reread()
        migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED

    def test_a_shallow_clone_holding_the_sync_and_its_parent_is_read(
        self, site, migration, tmp_path
    ):
        site.git(site.root, "init", "-q")
        site.write("README.md", "An instance.\n")
        site.commit("init")
        site.degraded()
        clone = tmp_path / "clone"
        site.git(tmp_path, "clone", "-q", "--depth", "3", f"file://{site.root}", str(clone))
        assert (clone / ".git" / "shallow").is_file()
        migration.apply(clone)
        assert (clone / PAGE_FILE).read_text(encoding="utf-8") == STORED

    def test_the_restored_line_keeps_the_units_lineage_and_license(self, site, migration):
        parent = work_hash("https://x.com/i/status/1")
        site.landed()
        site.synced()
        site.write(PAGE_FILE, FLATTENED)
        write_ledger(site.root, site.reread_line(parent=parent, depth=1, http_shared=True))
        site.commit("run: reruns drained")
        migration.apply(site.root)
        page = live(site.root)[PAGE_HASH]
        assert (page.parent, page.depth, page.http_shared) == (parent, 1, True)

    def test_a_copy_that_is_not_utf_8_still_counts(self, site, migration):
        site.landed()
        site.synced()
        site.write(PAGE_FILE, FLATTENED.encode() + b"caf\xe9\n")
        write_ledger(site.root, site.reread_line())
        site.commit("run: reruns drained")
        (restored,) = migration.apply(site.root).actions
        assert "the re-read held 0 fence line(s)" in restored

    @pytest.mark.parametrize(
        "fields",
        [
            {"engine": "0.2.3"},
            {"via": "migration-17"},
            {"via": None, "rerun": False},
            {"kind": Kind.X},
            {"kind": Kind.GITHUB},
            {"job": Job.ASSET, "parent": POST_HASH, "depth": 1},
        ],
    )
    def test_is_no_member(self, site, migration, fields):
        site.landed()
        site.synced()
        site.write(PAGE_FILE, FLATTENED)
        write_ledger(site.root, dataclasses.replace(site.reread_line(), **fields))
        site.commit("run: a landing of another shape")
        before = ledger_text(site.root)
        report = migration.apply(site.root)
        assert ledger_text(site.root) == before
        assert report.actions == report.skipped == []

    def test_a_paper_is_a_member(self, site, migration):
        site.landed()
        site.synced()
        site.write(PAGE_FILE, FLATTENED)
        write_ledger(site.root, site.reread_line(kind=Kind.PAPER))
        site.commit("run: reruns drained")
        (restored,) = migration.apply(site.root).actions
        assert restored.startswith(f"{PAGE_ITEM}: restored {PAGE_FILE}")

    def test_a_second_apply_finds_nothing(self, site, migration):
        site.degraded()
        migration.apply(site.root)
        before = ledger_text(site.root)
        report = migration.apply(site.root)
        assert ledger_text(site.root) == before
        assert report.actions == report.skipped == []

    def test_every_page_is_judged_whatever_came_before_it(self, site, migration):
        # A re-read whose file is gone, read first, and one that degraded.
        other_url = "https://docs.example.test/guide/missing"
        other_file = f"enrichment/{PAGE_ITEM}/web-{work_hash(other_url)[:6]}.md"
        site.git(site.root, "init", "-q")
        write_ledger(
            site.root,
            line(
                other_url,
                Status.DONE,
                engine="0.2.2",
                kind=Kind.WEB,
                item=PAGE_ITEM,
                via="migration-18",
                rerun=True,
                path=other_file,
            ),
        )
        site.degraded()
        report = migration.apply(site.root)
        assert len(report.skipped) == 1
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == STORED


class TestPagesHistoryCannotAnswer:
    def test_a_page_with_no_landing_at_the_sync_commit_is_named(self, site, migration):
        # A thin park the re-read landed: nothing stood before it.
        site.git(site.root, "init", "-q")
        write_corpus(site.root, item=PAGE_ITEM, urls=(PAGE_URL,))
        write_ledger(site.root, page_line(Status.MANUAL, engine="0.1.9", reason="thin-extraction"))
        site.commit("run: parked thin")
        site.synced()
        site.reread()
        report = migration.apply(site.root)
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == FLATTENED
        (skip,) = report.skipped
        assert skip.what == f"{PAGE_ITEM}: {PAGE_FILE} of {PAGE_URL}"
        assert skip.why.startswith("no landing of it stood at the 0.2.2 sync commit ")
        assert skip.why.endswith("; the page was left as the re-read stored it")

    def test_a_file_the_sync_commit_never_held_is_named(self, site, migration):
        site.landed()
        (site.root / PAGE_FILE).unlink()
        site.synced()
        site.reread()
        (skip,) = migration.apply(site.root).skipped
        assert skip.why.startswith(f"no file stood at {PAGE_FILE} in the 0.2.2 sync commit ")

    def test_a_sync_commit_holding_the_reread_already_is_named(self, site, migration):
        site.landed()
        site.synced(commit=False)
        site.reread()
        (skip,) = migration.apply(site.root).skipped
        assert "already holds its re-read" in skip.why
        assert (site.root / PAGE_FILE).read_text(encoding="utf-8") == FLATTENED

    def test_a_page_whose_file_is_gone_is_named(self, site, migration):
        site.degraded()
        (site.root / PAGE_FILE).unlink()
        (skip,) = migration.apply(site.root).skipped
        assert skip.why.startswith(f"its 0.2.2 re-read landed at {PAGE_FILE}, and no file")
        assert not (site.root / PAGE_FILE).exists()

    def test_a_page_no_live_item_claims_is_named(self, site, migration):
        site.degraded()
        (site.root / "corpus" / PAGE_ITEM[:4] / f"{PAGE_ITEM}.md").unlink()
        (skip,) = migration.apply(site.root).skipped
        assert skip.why.startswith("no live corpus item claims it")

    def test_a_copy_git_cannot_read_back_is_named(self, site, migration, monkeypatch):
        site.degraded()
        monkeypatch.setattr(migration_20, "checkout_bytes", lambda *_args: None)
        (skip,) = migration.apply(site.root).skipped
        assert f"git holds {PAGE_FILE} at " in skip.why
        assert "but could not read it" in skip.why

    def test_outside_a_repository_no_page_is_compared(self, site, migration):
        site.degraded()
        shutil.rmtree(site.root / ".git")
        (skip,) = migration.apply(site.root).skipped
        assert skip.what == "the 1 page(s) a 0.2.2 re-read replaced"
        assert "git answered nothing here" in skip.why
        assert skip.why.endswith("; none was compared")

    def test_history_that_never_recorded_the_sync_is_named(self, site, migration):
        site.landed()
        write_ledger(site.root, site.reread_line())
        site.write(PAGE_FILE, FLATTENED)
        site.commit("run: a re-read with no sync on record")
        (skip,) = migration.apply(site.root).skipped
        assert skip.what == "the 1 page(s) a 0.2.2 re-read replaced"
        assert skip.why.startswith("no commit records migration 18 in state/migrations.jsonl")

    def test_a_shallow_clone_says_its_depth_may_hide_the_sync(self, site, migration, tmp_path):
        site.degraded()
        clone = tmp_path / "clone"
        site.git(tmp_path, "clone", "-q", "--depth", "1", f"file://{site.root}", str(clone))
        (skip,) = migration.apply(clone).skipped
        assert "this clone is shallow" in skip.why


class TestTolerantRead:
    def test_an_unreadable_line_is_named_and_the_rest_restored(self, field, migration):
        field.retired_and_committed()
        path = field.root / "state" / "enrichment-ledger.jsonl"
        path.write_text('{"hash": "torn\n\n' + path.read_text(encoding="utf-8"))
        report = migration.apply(field.root)
        assert live(field.root)[VIDEO_HASH].status is Status.DONE
        (skip,) = report.skipped
        assert skip.what == "ledger line 1"
        assert "migration 20 skipped it" in skip.why

    def test_an_instance_with_no_ledger_reports_nothing(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == report.skipped == []


class TestShipped:
    def test_sync_runs_it_after_nineteen(self, field):
        field.retired_and_committed()
        applied = run_pending(
            field.root, today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.3"
        )
        numbers = [migration.number for migration in applied]
        assert numbers[-2:] == [19, 20]
        assert live(field.root)[VIDEO_HASH].via == "migration-20"
