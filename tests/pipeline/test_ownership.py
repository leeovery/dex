"""Tests for pipeline/ownership.py: which live corpus item claims a work unit."""

import datetime

from dex_engine.pipeline.ownership import corpus_owners, unit_owners, work_identity
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

DRIVERS = default_drivers()

X_SHARED = "https://x.com/carol/status/300"  # a share spelling
X_KEYED = "https://x.com/i/status/300"  # its id-keyed identity


def write_item(root, item_id, *, urls=(), media=()):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    url_lines = "".join(f"  - {url}\n" for url in urls)
    media_lines = "".join(f"  - {name}\n" for name in media)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-05-01\n"
        + (f"urls:\n{url_lines}" if url_lines else "")
        + (f"media:\n{media_lines}" if media_lines else "")
        + "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


class TestWorkIdentity:
    def test_a_url_keys_by_its_canonical_form(self):
        assert work_identity(X_SHARED, DRIVERS) == work_hash(X_KEYED)

    def test_a_url_no_driver_can_canonicalize_keys_raw(self):
        # Seeding parks such a URL under the raw hash; ownership has to
        # answer under the same key or the parked unit reads as unclaimed.
        bad = "https://[oops"
        assert work_identity(bad, DRIVERS) == work_hash(bad)


class TestCorpusOwners:
    def test_a_listed_url_is_claimed_under_its_current_identity(self, tmp_path):
        write_item(tmp_path, "2026-05-01-item-a1b2c3", urls=[X_SHARED])
        owners = corpus_owners(tmp_path, DRIVERS)
        assert owners[work_hash(X_KEYED)] == "2026-05-01-item-a1b2c3"

    def test_media_files_are_claimed_under_their_file_key(self, tmp_path):
        write_item(tmp_path, "2026-05-01-item-a1b2c3", media=["media/a1b2c3/talk.pdf"])
        owners = corpus_owners(tmp_path, DRIVERS)
        assert owners[work_hash("file:media/a1b2c3/talk.pdf")] == "2026-05-01-item-a1b2c3"

    def test_an_unlisted_url_is_claimed_by_nobody(self, tmp_path):
        write_item(tmp_path, "2026-05-01-item-a1b2c3", urls=[X_SHARED])
        assert work_hash("https://a.test/elsewhere") not in corpus_owners(tmp_path, DRIVERS)

    def test_two_items_listing_one_url_settle_as_seeding_does(self, tmp_path):
        write_item(tmp_path, "2026-05-01-second-bbbbbb", urls=[X_SHARED])
        write_item(tmp_path, "2026-05-01-first-aaaaaa", urls=[X_SHARED])
        owners = corpus_owners(tmp_path, DRIVERS)
        assert owners[work_hash(X_KEYED)] == "2026-05-01-first-aaaaaa"

    def test_one_unreadable_item_does_not_unclaim_the_rest(self, tmp_path):
        write_item(tmp_path, "2026-05-01-item-a1b2c3", urls=[X_SHARED])
        broken = tmp_path / "corpus" / "2026" / "2026-05-01-broken-ffffff.md"
        broken.write_text("not frontmatter at all\n", encoding="utf-8")
        assert corpus_owners(tmp_path, DRIVERS)[work_hash(X_KEYED)] == "2026-05-01-item-a1b2c3"

    def test_an_un_pulled_repo_has_no_claims(self, tmp_path):
        assert corpus_owners(tmp_path, DRIVERS) == {}


def entry(unit_hash, *, item, url=None, parent=None, depth=None, job=None):  # noqa: PLR0913 — one keyword per ledger identity slot
    return LedgerEntry(
        hash=unit_hash,
        url=url or f"https://example.test/{unit_hash}",
        item=item,
        kind=Kind.WEB,
        status=Status.DONE,
        engine="0.2.0",
        date=datetime.date(2026, 8, 1),
        job=job,
        parent=parent,
        depth=depth,
    )


LIVE = "2026-05-01-live-a1b2c3"
DEAD = "2026-05-01-dead-a1b2c3"  # renamed away: same shortid, no corpus file


class TestUnitOwners:
    """The one resolution: claims, then the parent chain, then the string."""

    def _chain(self, tmp_path, *, claimed: bool):
        """A frontmatter-claimed unit with a harvested child and grandchild.

        Every line spells the DEAD id — the rename never edits the ledger
        — and only the depth-0 unit's URL appears in any frontmatter.
        """
        if claimed:
            write_item(tmp_path, LIVE, urls=[X_SHARED])
        root_hash = work_hash(X_KEYED)
        return {
            root_hash: entry(root_hash, item=DEAD, url=X_KEYED),
            "b" * 10: entry("b" * 10, item=DEAD, parent=root_hash, depth=1),
            "c" * 10: entry("c" * 10, item=DEAD, parent="b" * 10, depth=2),
        }

    def test_a_child_resolves_through_its_parent_chain(self, tmp_path):
        entries = self._chain(tmp_path, claimed=True)
        owners = unit_owners(tmp_path, entries, DRIVERS)
        assert owners[work_hash(X_KEYED)] == (LIVE,)
        assert owners["b" * 10] == (LIVE,)  # harvested child, one hop
        assert owners["c" * 10] == (LIVE,)  # grandchild, the whole chain

    def test_a_media_unit_resolves_through_its_source_unit(self, tmp_path):
        write_item(tmp_path, LIVE, urls=[X_SHARED])
        root_hash = work_hash(X_KEYED)
        entries = {
            root_hash: entry(root_hash, item=DEAD, url=X_KEYED),
            "d" * 10: entry("d" * 10, item=DEAD, parent=root_hash, depth=1, job=Job.MEDIA),
        }
        assert unit_owners(tmp_path, entries, DRIVERS)["d" * 10] == (LIVE,)

    def test_an_unclaimed_chain_keeps_the_stored_string(self, tmp_path):
        # Nothing live claims any hash in the chain: the fallback is the
        # id each line was written under — lint's ghost finding to report,
        # never this map's to reassign.
        entries = self._chain(tmp_path, claimed=False)
        owners = unit_owners(tmp_path, entries, DRIVERS)
        assert owners["c" * 10] == (DEAD,)

    def test_a_live_stored_item_answers_even_unlisted(self, tmp_path):
        # The owner removed the URL from the frontmatter it was seeded
        # from; the unit still belongs to the item whose file exists.
        write_item(tmp_path, LIVE, urls=[])
        entries = {"e" * 10: entry("e" * 10, item=LIVE)}
        assert unit_owners(tmp_path, entries, DRIVERS)["e" * 10] == (LIVE,)

    def test_a_parent_cycle_costs_a_lookup_never_the_run(self, tmp_path):
        entries = {
            "a" * 10: entry("a" * 10, item=DEAD, parent="b" * 10, depth=1),
            "b" * 10: entry("b" * 10, item=DEAD, parent="a" * 10, depth=2),
        }
        owners = unit_owners(tmp_path, entries, DRIVERS)
        assert owners["a" * 10] == (DEAD,)
        assert owners["b" * 10] == (DEAD,)
