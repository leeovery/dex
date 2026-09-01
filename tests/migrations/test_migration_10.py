"""Tests for migration 10: compile the map artifacts at sync time."""

import datetime
import json

import pytest

from dex_engine.instance_map import write_map
from dex_engine.migrations import append_applied, log_path, read_applied, run_pending
from dex_engine.migrations.migration_10 import build
from dex_engine.pipeline.types import Instance

TODAY = datetime.date(2026, 9, 1)
NOW = datetime.datetime(2026, 9, 1, 9, 0, 0, 500000, tzinfo=datetime.UTC)

A = "2026-01-05-alpha-aaaaaa"
B = "2026-02-10-bravo-bbbbbb"
HAND_INDEX = "# Index\n\nHand-maintained catalog prose, drift and all.\n"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def write_taxonomy(root, payload) -> None:
    path = root / "state" / "taxonomy.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def write_members(root, payload) -> None:
    path = root / "state" / "entity-members.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def write_item(root, item_id: str, *, date: str) -> None:
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\n"
        f"date: {date}\n"
        "kinds: [web]\nstatus: raw\nenrichment: []\n---\nnote\n"
    )


def write_page(root, name: str, body: str) -> None:
    path = root / "wiki" / "topics" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntopic: {name}\ngenerated: 2026-08-01\n---\n{body}")


def seed(root) -> None:
    """A real-shaped instance: two topics, one entity, a hand-written index."""
    write_taxonomy(
        root,
        {
            "topics": {
                "brewing": {"description": "Brew technique.", "items": [A, B]},
                "grinders": {"description": "Grinder gear.", "items": [B]},
                "uncategorized-shares": {"description": "Ledger.", "items": []},
            },
            "entities": {"hoffmann": {"kind": "person", "raw": ["james-hoffmann"]}},
        },
    )
    write_members(root, {"hoffmann": [A]})
    write_item(root, A, date="2026-01-05")
    write_item(root, B, date="2026-02-10")
    write_page(root, "brewing", "See [[grinders]].")
    (root / "wiki" / "index.md").write_text(HAND_INDEX)


def read_map(root) -> dict:
    return json.loads((root / "state" / "map.json").read_text())


def fingerprint(path):
    stat = path.stat()
    return (stat.st_ino, stat.st_mtime_ns)


class TestApply:
    def test_the_artifacts_exist_and_answer_the_taxonomy(self, tmp_path, migration):
        seed(tmp_path)
        report = migration.apply(tmp_path)
        payload = read_map(tmp_path)
        assert payload["topics"]["brewing"] == {
            "description": "Brew technique.",
            "items": [A, B],
            "count": 2,
            "has_page": True,
            "newest": "2026-02-10",
        }
        assert "uncategorized-shares" not in payload["topics"]
        assert payload["entities"]["hoffmann"]["count"] == 1
        assert {"type": "wikilink", "source": "brewing", "target": "grinders"} in payload["graph"]
        assert report.actions == [
            "compiled state/map.json + wiki/index.md: 2 topics, 1 entities, 3 edges"
        ]
        assert report.skipped == []
        assert report.anomalies == []

    def test_the_model_written_index_is_replaced_whole(self, tmp_path, migration):
        seed(tmp_path)
        migration.apply(tmp_path)
        index = (tmp_path / "wiki" / "index.md").read_text()
        assert "Hand-maintained" not in index
        assert "Rendered by `bin/dex map`" in index
        assert "[[brewing]] — Brew technique. (2 items)" in index

    def test_a_pre_taxonomy_instance_gets_honestly_empty_artifacts(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert read_map(tmp_path) == {"topics": {}, "entities": {}, "graph": []}
        assert (tmp_path / "wiki" / "index.md").read_text().startswith("# Index\n")
        assert report.actions == [
            "compiled state/map.json + wiki/index.md: 0 topics, 0 entities, 0 edges"
        ]
        assert report.anomalies == []

    @pytest.mark.parametrize(
        ("taxonomy", "members"),
        [
            ("{not json", "{}"),
            ({"topics": {"brewing": []}, "entities": {}}, "{}"),
            ({"topics": {}, "entities": {}}, {"hoffmann": "not-a-list"}),
        ],
    )
    def test_a_malformed_judgment_file_is_an_anomaly_never_a_raise(
        self, tmp_path, migration, taxonomy, members
    ):
        # The repair is session judgment on the named file; a raise here
        # would stop every future sync at step 0, ahead of the machinery
        # refresh that ships the repair procedure.
        write_taxonomy(tmp_path, taxonomy)
        write_members(tmp_path, members)
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "index.md").write_text(HAND_INDEX)
        report = migration.apply(tmp_path)
        assert not (tmp_path / "state" / "map.json").exists()
        assert (tmp_path / "wiki" / "index.md").read_text() == HAND_INDEX
        assert report.actions == []
        (anomaly,) = report.anomalies
        assert anomaly.startswith("the map compile refused: ")
        assert "`bin/dex map`" in anomaly

    def test_a_second_apply_writes_nothing_and_reports_nothing(self, tmp_path, migration):
        seed(tmp_path)
        migration.apply(tmp_path)
        map_path = tmp_path / "state" / "map.json"
        index_path = tmp_path / "wiki" / "index.md"
        before = (fingerprint(map_path), fingerprint(index_path))
        report = migration.apply(tmp_path)
        assert (fingerprint(map_path), fingerprint(index_path)) == before
        assert report.actions == []
        assert report.anomalies == []

    def test_a_reapplication_after_inputs_moved_recompiles_honestly(self, tmp_path, migration):
        # A log-race re-application runs against an instance that moved on;
        # both artifacts are derived whole, so recompiling is the safe act.
        seed(tmp_path)
        migration.apply(tmp_path)
        write_taxonomy(
            tmp_path,
            {
                "topics": {"brewing": {"description": "Brew technique.", "items": [A]}},
                "entities": {},
            },
        )
        write_members(tmp_path, {})
        report = migration.apply(tmp_path)
        payload = read_map(tmp_path)
        assert list(payload["topics"]) == ["brewing"]
        assert payload["entities"] == {}
        assert report.actions == [
            "compiled state/map.json + wiki/index.md: 1 topics, 0 entities, 0 edges"
        ]

    def test_an_interrupted_apply_resumes_on_the_missing_artifact(self, tmp_path, migration):
        seed(tmp_path)
        migration.apply(tmp_path)
        map_before = fingerprint(tmp_path / "state" / "map.json")
        (tmp_path / "wiki" / "index.md").unlink()
        report = migration.apply(tmp_path)
        assert fingerprint(tmp_path / "state" / "map.json") == map_before
        assert (tmp_path / "wiki" / "index.md").read_text().startswith("# Index\n")
        assert report.actions == ["compiled wiki/index.md: 2 topics, 1 entities, 3 edges"]


class TestConvergence:
    """One compile, one byte form: the trigger finds nothing left to write."""

    def test_the_compile_trigger_reproduces_the_migrated_bytes(self, tmp_path, migration):
        seed(tmp_path)
        migration.apply(tmp_path)
        map_path = tmp_path / "state" / "map.json"
        index_path = tmp_path / "wiki" / "index.md"
        migrated = (map_path.read_bytes(), index_path.read_bytes())
        write_map(Instance(root=tmp_path))
        assert (map_path.read_bytes(), index_path.read_bytes()) == migrated


class TestRecording:
    def test_recorded_in_the_log_and_never_rerun(self, tmp_path):
        seed(tmp_path)
        path = log_path(tmp_path)
        for number in range(1, 10):
            append_applied(path, number=number, engine="0.1.12", date=TODAY)
        applied = run_pending(
            tmp_path, today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0"
        )
        assert [one.number for one in applied] == [10]
        assert 10 in read_applied(path)
        assert (tmp_path / "state" / "map.json").exists()
        again = run_pending(tmp_path, today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")
        assert again == []
