"""Tests for migration 15: re-read the github links the old dispatch misread."""

import datetime
import json

import pytest

from dex_engine.migrations import migration_15
from dex_engine.migrations.migration_15 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.enrichment import render_enrichment
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 26)
NOW = datetime.datetime(2026, 9, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.2"
FETCHED = datetime.date(2026, 9, 1)

ITEM = "2026-09-01-toolkit-notes-abc123"

TREE = "https://github.com/acme/toolkit/tree/main/docs/guides"
BRANCH_ROOT = "https://github.com/acme/toolkit/tree/release-2"
COMMIT = "https://github.com/acme/toolkit/commit/5c1e0f0a9d3b7e2c4f6a8b0d1e3f5a7c9b1d3e5f"
ATTACHED_PDF = "https://github.com/user-attachments/files/4242/field-notes.pdf"
ATTACHED_PICTURE = "https://github.com/user-attachments/assets/0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
BLOB_DIR = "https://github.com/acme/toolkit/blob/main/examples"
NEW_ISSUE = "https://github.com/acme/toolkit/issues/new"

REPO = "https://github.com/acme/toolkit"
ISSUE = "https://github.com/acme/toolkit/issues/7"
BLOB_FILE = "https://github.com/acme/toolkit/blob/main/README.md"

REPO_META: dict[str, str | int | None] = {
    "title": "acme/toolkit",
    "description": "Tools for the toolkit.",
    "stars": 311,
}
OLD_REFUSAL = "still blocked after 5 attempts — gh api returned an unexpected shape"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def ledger_path(root):
    return root / "state" / "enrichment-ledger.jsonl"


def write_ledger(root, *entries):
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ledger.to_line(e) + "\n" for e in entries))
    return path


def write_item(root, item_id=ITEM, urls=(TREE,)):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-09-01\n"
        f"urls:\n{listing}"
        "kinds: [github]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def output_path(url, item=ITEM, kind=Kind.GITHUB):
    return f"enrichment/{item}/{kind.value}-{work_hash(url)[:6]}.md"


def write_output(root, url, *, meta=REPO_META, body="# toolkit\n\nThe root README.", at=None):
    """An enrichment file for ``url``, written by the real renderer."""
    path = root / (at or output_path(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_enrichment(url, FETCHED, dict(meta), body), encoding="utf-8")
    return path


def entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    url,
    *,
    status=Status.DONE,
    kind=Kind.GITHUB,
    item=ITEM,
    path="stored",
    reason=None,
    parent=None,
    depth=None,
    via=None,
    job=None,
    at=NOW,
    http_shared=False,
):
    if path == "stored":
        path = output_path(url, item, kind) if status is Status.DONE else None
    if reason is None and status in (Status.DEAD, Status.MANUAL, Status.BLOCKED):
        reason = "HTTP 404"
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine="0.2.1",
        date=FETCHED,
        at=at,
        attempts=1 if status is Status.BLOCKED else None,
        path=path,
        reason=reason,
        parent=parent,
        depth=depth,
        via=via,
        job=job,
        http_shared=http_shared,
    )


def seeds(root, url=None):
    lines = [
        json.loads(line)
        for line in ledger_path(root).read_text().splitlines()
        if line.startswith("{")
    ]
    return [
        line
        for line in lines
        if line.get("via") == "migration-15" and (url is None or line["url"] == url)
    ]


def assert_nothing_done(report, root, before):
    assert report.actions == []
    assert ledger_path(root).read_text() == before


class TestCollapsedLinks:
    """A link the old dispatch read as the repo landed the root README: gone, then re-read."""

    @pytest.mark.parametrize(
        "url",
        [
            TREE,
            BRANCH_ROOT,
            COMMIT,
            "https://github.com/acme/toolkit/raw/main/notes.txt",
            "https://github.com/acme/toolkit/releases/tag/v1.0",
            "https://github.com/acme/toolkit/releases/download/v1.0/manual.pdf",
            "https://github.com/acme/toolkit/releases/latest/download/manual.pdf",
            "https://github.com/acme/toolkit/wiki/Design",
            "https://github.com/acme/toolkit/issues",
            "https://github.com/acme/toolkit/blob",
        ],
    )
    def test_the_root_readme_is_deleted_and_the_link_requeued(self, tmp_path, migration, url):
        write_item(tmp_path, urls=(url,))
        stored = write_output(tmp_path, url)
        write_ledger(tmp_path, entry(url))
        report = migration.apply(tmp_path)
        assert not stored.exists()
        assert report.actions[1] == (
            f"deleted {output_path(url)}: the repo's root README the old dispatch stored for "
            f"{url}, which is not what the link points at — the rerun replaces it"
        )
        [seed] = seeds(tmp_path, url)
        assert seed["status"] == "queued"
        assert seed["kind"] == "github"
        assert seed["item"] == ITEM
        assert seed["rerun"] is True

    def test_the_seed_is_stamped_and_keeps_the_units_identity(self, tmp_path, migration):
        write_item(tmp_path)
        write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE))
        migration.apply(tmp_path)
        lines = ledger_path(tmp_path).read_text().splitlines()
        seed = ledger.from_line(lines[-1])
        assert seed == LedgerEntry(
            hash=work_hash(TREE),
            url=TREE,
            item=ITEM,
            kind=Kind.GITHUB,
            status=Status.QUEUED,
            engine=ENGINE,
            date=TODAY,
            at=NOW,
            via="migration-15",
            rerun=True,
        )

    def test_a_harvested_links_seed_keeps_its_lineage(self, tmp_path, migration):
        parent = "https://example.test/post"
        write_item(tmp_path, urls=(parent,))
        write_output(tmp_path, TREE)
        write_ledger(
            tmp_path,
            entry(parent, kind=Kind.WEB),
            entry(TREE, parent=work_hash(parent), depth=1, via="harvest"),
        )
        migration.apply(tmp_path)
        [seed] = seeds(tmp_path, TREE)
        assert (seed["parent"], seed["depth"], seed["item"]) == (work_hash(parent), 1, ITEM)

    def test_a_link_shared_over_http_keeps_its_license(self, tmp_path, migration):
        # The flag rides every superseding line: it is what licenses the
        # TLS-failure http fallback, and only the admission door saw it.
        write_item(tmp_path)
        write_ledger(tmp_path, entry(TREE, http_shared=True))
        migration.apply(tmp_path)
        [seed] = seeds(tmp_path, TREE)
        assert seed["http_shared"] is True

    def test_a_line_whose_output_is_gone_is_requeued_with_nothing_to_delete(
        self, tmp_path, migration
    ):
        # What an apply interrupted between its delete and its seed leaves:
        # a done line over no file. The rerun is its repair either way.
        write_item(tmp_path)
        write_ledger(tmp_path, entry(TREE))
        report = migration.apply(tmp_path)
        assert len(seeds(tmp_path, TREE)) == 1
        assert len(report.actions) == 1  # the summary; nothing was deleted

    @pytest.mark.parametrize(
        ("meta", "body"),
        [
            # The directory route: its README and listing, no repo metadata.
            ({"title": "acme/toolkit/docs/guides"}, "## README\n\nguides\n\n## Contents"),
            # The repo route at a ref the link named — the ref is recorded.
            ({**REPO_META, "ref": "main"}, "# toolkit"),
            # The blob route, for a tree link that named a file.
            ({"file": "docs/guides"}, "```\ntext\n```"),
            # An owner's own heal.
            ({"title": "Guides, read by hand"}, "what the directory holds"),
        ],
    )
    def test_any_other_stored_output_stands(self, tmp_path, migration, meta, body):
        write_item(tmp_path)
        stored = write_output(tmp_path, TREE, meta=meta, body=body)
        write_ledger(tmp_path, entry(TREE))
        before = ledger_path(tmp_path).read_text()
        report = migration.apply(tmp_path)
        assert stored.exists()
        assert_nothing_done(report, tmp_path, before)

    def test_a_stale_readme_under_the_engines_name_goes_and_the_heal_stays(
        self, tmp_path, migration
    ):
        # The drain keeps a larger stored body at the engine's name, so a
        # root README left there would outlive the rerun even beside a heal.
        write_item(tmp_path)
        heal = f"enrichment/{ITEM}/guides-by-hand.md"
        healed = write_output(tmp_path, TREE, meta={"title": "Guides"}, body="read", at=heal)
        stale = write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE, path=heal))
        migration.apply(tmp_path)
        assert healed.exists()
        assert not stale.exists()
        assert len(seeds(tmp_path, TREE)) == 1

    def test_a_root_readme_at_the_stored_path_goes_too(self, tmp_path, migration):
        write_item(tmp_path)
        elsewhere = f"enrichment/{ITEM}/toolkit-readme.md"
        stored = write_output(tmp_path, TREE, at=elsewhere)
        write_ledger(tmp_path, entry(TREE, path=elsewhere))
        migration.apply(tmp_path)
        assert not stored.exists()

    def test_a_line_naming_no_path_still_loses_the_readme_at_the_engines_name(
        self, tmp_path, migration
    ):
        write_item(tmp_path)
        stale = write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE, path=None))
        migration.apply(tmp_path)
        assert not stale.exists()

    def test_a_landing_that_stands_never_stops_the_rest(self, tmp_path, migration):
        write_item(tmp_path, urls=(TREE, COMMIT))
        write_output(tmp_path, TREE, meta={"title": "acme/toolkit/docs/guides"})
        write_output(tmp_path, COMMIT)
        write_ledger(tmp_path, entry(TREE), entry(COMMIT))
        migration.apply(tmp_path)
        assert [seed["url"] for seed in seeds(tmp_path)] == [COMMIT]

    def test_another_units_file_under_the_engines_name_is_no_proof(self, tmp_path, migration):
        write_item(tmp_path)
        neighbour = write_output(tmp_path, "https://github.com/acme/other/tree/main/x")
        neighbour.rename(neighbour.with_name(output_path(TREE).rsplit("/", 1)[-1]))
        write_ledger(tmp_path, entry(TREE))
        before = ledger_path(tmp_path).read_text()
        report = migration.apply(tmp_path)
        assert (tmp_path / output_path(TREE)).exists()
        assert_nothing_done(report, tmp_path, before)

    def test_an_unreadable_output_is_left_and_named(self, tmp_path, migration):
        write_item(tmp_path)
        garbled = tmp_path / output_path(TREE)
        garbled.parent.mkdir(parents=True)
        garbled.write_bytes(b"\xff\xfe not text")
        write_ledger(tmp_path, entry(TREE))
        before = ledger_path(tmp_path).read_text()
        report = migration.apply(tmp_path)
        assert garbled.exists()
        assert_nothing_done(report, tmp_path, before)
        [skip] = report.skipped
        assert skip.what == output_path(TREE)
        assert "could not be read" in skip.why

    def test_a_stored_path_outside_the_root_is_never_touched(self, tmp_path, migration):
        root = tmp_path / "instance"
        outside = write_output(tmp_path, TREE, at="outside.md")
        write_item(root)
        write_ledger(root, entry(TREE, path="../outside.md"))
        migration.apply(root)
        assert outside.exists()
        assert len(seeds(root, TREE)) == 1


class TestAttachments:
    """An attachment the old dispatch read as a repo: requeued for the fixed correction."""

    @pytest.mark.parametrize("url", [ATTACHED_PDF, ATTACHED_PICTURE])
    def test_a_dead_attachment_is_requeued_as_github_work(self, tmp_path, migration, url):
        write_item(tmp_path, urls=(url,))
        write_ledger(tmp_path, entry(url, status=Status.DEAD))
        report = migration.apply(tmp_path)
        [seed] = seeds(tmp_path, url)
        assert (seed["kind"], seed["status"]) == ("github", "queued")
        assert report.skipped == []

    def test_a_hand_heal_stays_for_the_landing_to_retire(self, tmp_path, migration):
        # Only a hand heal can have landed one done: the owner's reading is
        # merely incomplete, so it stands as the copy a failed rerun keeps.
        write_item(tmp_path, urls=(ATTACHED_PDF,))
        heal = f"enrichment/{ITEM}/field-notes.md"
        healed = write_output(tmp_path, ATTACHED_PDF, meta={"title": "notes"}, at=heal)
        write_ledger(tmp_path, entry(ATTACHED_PDF, path=heal))
        report = migration.apply(tmp_path)
        assert healed.exists()
        assert len(seeds(tmp_path, ATTACHED_PDF)) == 1
        assert len(report.actions) == 1

    def test_the_namespace_is_read_whatever_its_case(self, tmp_path, migration):
        url = "https://github.com/User-Attachments/files/4242/field-notes.pdf"
        write_item(tmp_path, urls=(url,))
        write_ledger(tmp_path, entry(url, status=Status.DEAD))
        migration.apply(tmp_path)
        assert len(seeds(tmp_path, url)) == 1

    @pytest.mark.parametrize("status", [Status.MANUAL, Status.BLOCKED, Status.QUEUED])
    def test_any_other_status_is_left_to_the_drain_or_the_owner(self, tmp_path, migration, status):
        write_item(tmp_path, urls=(ATTACHED_PDF,))
        write_ledger(tmp_path, entry(ATTACHED_PDF, status=status))
        before = ledger_path(tmp_path).read_text()
        assert_nothing_done(migration.apply(tmp_path), tmp_path, before)


class TestDirectoryBlobs:
    """A blob link naming a directory escalated on the array answer: requeued."""

    def test_the_old_refusal_is_requeued(self, tmp_path, migration):
        write_item(tmp_path, urls=(BLOB_DIR,))
        write_ledger(tmp_path, entry(BLOB_DIR, status=Status.MANUAL, reason=OLD_REFUSAL))
        migration.apply(tmp_path)
        assert len(seeds(tmp_path, BLOB_DIR)) == 1

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (Status.MANUAL, "src/big.bin is larger than the contents API serves inline"),
            # Still retrying: the fixed driver reads it on the next attempt.
            (Status.BLOCKED, "gh api returned an unexpected shape"),
        ],
    )
    def test_any_other_blob_park_stands(self, tmp_path, migration, status, reason):
        write_item(tmp_path, urls=(BLOB_DIR,))
        write_ledger(tmp_path, entry(BLOB_DIR, status=status, reason=reason))
        before = ledger_path(tmp_path).read_text()
        assert_nothing_done(migration.apply(tmp_path), tmp_path, before)


class TestNotAnIssue:
    @pytest.mark.parametrize(
        "url", [NEW_ISSUE, "https://github.com/acme/toolkit/pull/new/feature-branch"]
    )
    def test_a_dead_link_with_no_number_is_requeued(self, tmp_path, migration, url):
        write_item(tmp_path, urls=(url,))
        write_ledger(tmp_path, entry(url, status=Status.DEAD))
        migration.apply(tmp_path)
        assert len(seeds(tmp_path, url)) == 1

    def test_a_numbered_issue_that_died_stays_dead(self, tmp_path, migration):
        write_item(tmp_path, urls=(ISSUE,))
        write_ledger(tmp_path, entry(ISSUE, status=Status.DEAD))
        before = ledger_path(tmp_path).read_text()
        assert_nothing_done(migration.apply(tmp_path), tmp_path, before)


class TestNonMembers:
    """What the old dispatch read right, and what the fixed engine landed, stands."""

    @pytest.mark.parametrize(
        "line",
        [
            entry(REPO),
            entry(ISSUE),
            entry(BLOB_FILE),
            entry("https://github.com/octomaint"),
            entry("https://gist.github.com/octomaint/0f1e2d3c4b5a69788796a5b4c3d2e1f0"),
            # github.com's own pages were never github work.
            entry("https://github.com/topics/rust", kind=Kind.WEB),
            # The fixed engine's corrections: the attachment is file or media
            # work now, and a commit link parks.
            entry(ATTACHED_PDF, kind=Kind.FILE),
            entry(ATTACHED_PICTURE, kind=Kind.WEB, job=Job.MEDIA),
            entry(COMMIT, status=Status.MANUAL, reason="github /commit/ link — no route"),
            # A media child whose URL happens to sit on github.com.
            entry(TREE, job=Job.MEDIA),
            # Already requeued: an interrupted apply's, or the owner's own.
            entry(TREE, status=Status.QUEUED),
        ],
    )
    def test_it_stands(self, tmp_path, migration, line):
        write_item(tmp_path, urls=(line.url,))
        write_output(tmp_path, line.url, at=line.path)
        write_ledger(tmp_path, line)
        before = ledger_path(tmp_path).read_text()
        assert_nothing_done(migration.apply(tmp_path), tmp_path, before)


class TestOwnership:
    RENAMED = "2026-09-01-toolkit-guides-abc123"

    def test_a_renamed_item_is_found_through_its_claim(self, tmp_path, migration):
        write_item(tmp_path, item_id=self.RENAMED)
        stored = write_output(tmp_path, TREE)  # still under the old id
        write_ledger(tmp_path, entry(TREE))
        report = migration.apply(tmp_path)
        assert not stored.exists()
        [seed] = seeds(tmp_path, TREE)
        assert seed["item"] == self.RENAMED
        assert report.actions[0] == (
            "seeded 1 github rerun(s): links the old dispatch read as the repo's root README, "
            "as a repo named user-attachments, as a file when they named a directory, or as an "
            "issue when they named none — the fixed routes read what each points at; 1 "
            "re-attributed to the renamed item that claims them"
        )

    def test_the_new_items_engine_named_readme_goes_too(self, tmp_path, migration):
        write_item(tmp_path, item_id=self.RENAMED)
        moved = write_output(tmp_path, TREE, at=output_path(TREE, item=self.RENAMED))
        write_ledger(tmp_path, entry(TREE))
        migration.apply(tmp_path)
        assert not moved.exists()

    def test_a_harvested_link_follows_its_parent_to_the_renamed_item(self, tmp_path, migration):
        parent = "https://example.test/post"
        write_item(tmp_path, item_id=self.RENAMED, urls=(parent,))
        write_ledger(
            tmp_path,
            entry(parent, kind=Kind.WEB),
            entry(NEW_ISSUE, status=Status.DEAD, parent=work_hash(parent), depth=1),
        )
        migration.apply(tmp_path)
        [seed] = seeds(tmp_path, NEW_ISSUE)
        assert seed["item"] == self.RENAMED

    def test_an_excluded_item_is_named_and_left_whole(self, tmp_path, migration):
        stored = write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE))
        (tmp_path / "state" / "exclusions.tsv").write_text(
            f"2026-08-01-other-item-fff000\tduplicate\n\n{ITEM}\tnot for this dex\n"
        )
        report = migration.apply(tmp_path)
        assert stored.exists()
        assert seeds(tmp_path) == []
        [skip] = report.skipped
        assert skip.what == f"github re-read for {ITEM}"
        assert "state/exclusions.tsv: not for this dex" in skip.why

    def test_an_exclusion_with_no_stated_reason_says_so(self, tmp_path, migration):
        write_ledger(tmp_path, entry(NEW_ISSUE, status=Status.DEAD))
        (tmp_path / "state" / "exclusions.tsv").write_text(f"{ITEM}\n")
        [skip] = migration.apply(tmp_path).skipped
        assert "no reason recorded" in skip.why

    def test_unclaimed_work_is_named_and_the_rest_heals(self, tmp_path, migration):
        write_item(tmp_path, item_id=self.RENAMED, urls=(COMMIT,))
        write_ledger(
            tmp_path,
            entry(NEW_ISSUE, status=Status.DEAD),
            entry(COMMIT, item=self.RENAMED),
        )
        report = migration.apply(tmp_path)
        [skip] = report.skipped
        assert f"no live corpus item claims {NEW_ISSUE}" in skip.why
        assert [s["url"] for s in seeds(tmp_path)] == [COMMIT]


class TestDigestRepair:
    def write_digest(self, root, item=ITEM):
        digest = root / "state" / "digests" / f"{item}.md"
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.write_text("---\nid: x\n---\nfacts\n")

    def test_an_item_that_lost_a_root_readme_has_its_digest_repaired_this_run(
        self, tmp_path, migration
    ):
        write_item(tmp_path, urls=(TREE, COMMIT))
        write_output(tmp_path, TREE)
        write_output(tmp_path, COMMIT)
        self.write_digest(tmp_path)
        write_ledger(tmp_path, entry(TREE), entry(COMMIT))
        [repair] = migration.apply(tmp_path).skipped
        assert repair.what == f"state/digests/{ITEM}.md"
        assert repair.why == (
            f"written from the repo's root README the old dispatch stored for {TREE}, "
            f"{COMMIT} instead of what the link points at; that file is deleted and the link "
            f"re-fetches in this run — once it lands, re-digest {ITEM} from a fresh reading "
            "and carry none of the old digest's facts forward"
        )

    def test_every_digested_item_is_named_whatever_sorts_before_it(self, tmp_path, migration):
        first = "2026-08-01-earlier-item-aaa111"
        write_item(tmp_path, item_id=first, urls=(COMMIT,))
        write_item(tmp_path, urls=(TREE,))
        write_output(tmp_path, COMMIT, at=output_path(COMMIT, item=first))
        write_output(tmp_path, TREE)
        self.write_digest(tmp_path)
        write_ledger(tmp_path, entry(COMMIT, item=first), entry(TREE))
        assert [r.what for r in migration.apply(tmp_path).skipped] == [f"state/digests/{ITEM}.md"]

    def test_an_undigested_item_owes_no_repair(self, tmp_path, migration):
        write_item(tmp_path)
        write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE))
        assert migration.apply(tmp_path).skipped == []

    def test_a_requeue_that_deleted_nothing_owes_no_repair(self, tmp_path, migration):
        write_item(tmp_path, urls=(ATTACHED_PDF, TREE))
        self.write_digest(tmp_path)
        write_ledger(tmp_path, entry(ATTACHED_PDF, status=Status.DEAD), entry(TREE))
        report = migration.apply(tmp_path)
        assert len(seeds(tmp_path)) == 2
        assert report.skipped == []


class TestIdempotency:
    def members(self, root):
        write_item(root, urls=(TREE, BRANCH_ROOT, COMMIT, ATTACHED_PDF, BLOB_DIR, NEW_ISSUE))
        write_output(root, TREE)
        write_output(root, BRANCH_ROOT)
        write_output(root, COMMIT)
        return write_ledger(
            root,
            entry(TREE),
            entry(BRANCH_ROOT),
            entry(COMMIT),
            entry(ATTACHED_PDF, status=Status.DEAD),
            entry(BLOB_DIR, status=Status.MANUAL, reason=OLD_REFUSAL),
            entry(NEW_ISSUE, status=Status.DEAD),
        )

    def test_a_second_apply_finds_nothing(self, tmp_path, migration):
        path = self.members(tmp_path)
        assert len(seeds(tmp_path)) == 0
        migration.apply(tmp_path)
        assert len(seeds(tmp_path)) == 6
        after_first = path.read_text()
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert path.read_text() == after_first

    def test_a_second_apply_after_the_reruns_land_finds_nothing(self, tmp_path, migration):
        # What the fixed engine does with each seed: a directory landing, the
        # repo read at the ref the link named, a park, a kind correction.
        path = self.members(tmp_path)
        migration.apply(tmp_path)
        later = NOW + datetime.timedelta(minutes=5)
        write_output(tmp_path, TREE, meta={"title": "acme/toolkit/docs/guides"}, body="## README")
        write_output(tmp_path, BRANCH_ROOT, meta={**REPO_META, "ref": "release-2"})
        with path.open("a") as ledger_file:
            for landed in (
                entry(TREE, at=later, via="migration-15"),
                entry(BRANCH_ROOT, at=later, via="migration-15"),
                entry(COMMIT, status=Status.MANUAL, reason="no route", at=later),
                entry(ATTACHED_PDF, kind=Kind.FILE, at=later, via="sniff"),
                entry(BLOB_DIR, at=later, via="migration-15"),
                entry(NEW_ISSUE, status=Status.MANUAL, reason="no route", at=later),
            ):
                ledger_file.write(ledger.to_line(landed) + "\n")
        write_output(tmp_path, BLOB_DIR, meta={"title": "acme/toolkit/examples"}, body="x")
        after_drain = path.read_text()
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert path.read_text() == after_drain


class TestInterruption:
    def test_an_apply_cut_between_its_delete_and_its_seed_seeds_on_the_reapply(
        self, tmp_path, migration, monkeypatch
    ):
        # The rerun guard keeps a unit's stored file as done on every outcome
        # that is not content, so the delete has to come first — and the
        # re-apply has to find the unit by its done line alone, the file
        # already gone.
        write_item(tmp_path, urls=(COMMIT,))
        stored = write_output(tmp_path, COMMIT)
        write_ledger(tmp_path, entry(COMMIT))

        def cut(_path, _entry):
            raise OSError("killed mid-apply")

        with monkeypatch.context() as patched:
            patched.setattr(migration_15, "append", cut)
            with pytest.raises(OSError, match="killed mid-apply"):
                migration.apply(tmp_path)
        assert not stored.exists()
        assert seeds(tmp_path) == []

        report = migration.apply(tmp_path)
        [seed] = seeds(tmp_path, COMMIT)
        assert seed["status"] == "queued"
        assert len(report.actions) == 1  # the summary: nothing left to delete


class TestTolerance:
    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])

    def test_an_unparseable_line_is_named_and_never_stops_the_healing(self, tmp_path, migration):
        write_item(tmp_path)
        write_output(tmp_path, TREE)
        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json at all\n\n" + ledger.to_line(entry(TREE)) + "\n")
        report = migration.apply(tmp_path)
        assert len(seeds(tmp_path, TREE)) == 1
        assert [s.what for s in report.skipped] == ["ledger line 1"]
        assert "does not parse" in report.skipped[0].why

    def test_a_parse_skip_survives_a_memberless_ledger(self, tmp_path, migration):
        path = ledger_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{broken\n" + ledger.to_line(entry(REPO)) + "\n")
        report = migration.apply(tmp_path)
        assert [s.what for s in report.skipped] == ["ledger line 1"]

    def test_the_live_line_wins_whatever_the_file_order(self, tmp_path, migration):
        # A union merge can leave a hash's newest line mid-file: the done
        # line from before a requeue is not the unit's live state.
        write_item(tmp_path)
        write_output(tmp_path, TREE)
        earlier = NOW - datetime.timedelta(seconds=1)
        write_item(tmp_path, urls=(TREE, COMMIT))
        write_ledger(
            tmp_path,
            entry(TREE, status=Status.QUEUED),
            entry(TREE, at=earlier),
            entry(COMMIT),
        )
        migration.apply(tmp_path)
        assert [seed["url"] for seed in seeds(tmp_path)] == [COMMIT]

    def test_a_tie_is_broken_by_position(self, tmp_path, migration):
        write_item(tmp_path)
        write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE, status=Status.QUEUED), entry(TREE))
        migration.apply(tmp_path)
        assert len(seeds(tmp_path, TREE)) == 1

    def test_an_unpulled_corpus_deletes_nothing(self, tmp_path, migration):
        stored = write_output(tmp_path, TREE)
        write_ledger(tmp_path, entry(TREE))
        report = migration.apply(tmp_path)
        assert stored.exists()
        assert seeds(tmp_path) == []
        assert len(report.skipped) == 1
