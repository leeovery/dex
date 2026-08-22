"""Tests for pipeline/ownership.py: which live corpus item claims a work unit."""

from dex_engine.pipeline.ownership import corpus_owners, work_identity
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.urls import work_hash

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
