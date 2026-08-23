"""Tests for sync.py: pin flow, re-exec seams, migrations-first, template sync."""

import datetime
from pathlib import Path

import pytest

from dex_engine.migrations import AppliedMigration, log_path, read_applied
from dex_engine.pipeline.types import Instance, MigrationReport, Skipped
from dex_engine.sync import (
    PIN_FILE,
    ReleaseChannel,
    ReleaseCheckError,
    build_parser,
    main,
    read_pin,
    release_tags,
    run_sync,
    sync,
    write_pin,
)

RUNNING = "0.1.0"
TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)


def fixed_today() -> datetime.date:
    return TODAY


def fixed_now() -> datetime.datetime:
    return NOW


def listing_for(*tags: str) -> str:
    lines = []
    for i, tag in enumerate(tags):
        sha = f"{i:040x}"
        lines.append(f"{sha}\trefs/tags/{tag}")
        lines.append(f"{sha}\trefs/tags/{tag}^{{}}")
    return "\n".join(lines) + "\n"


def make_channel(listing: str = "", *, fail: bool = False):
    calls: dict[str, list] = {"ls": [], "exec": []}

    def ls_remote(url: str) -> str:
        calls["ls"].append(url)
        if fail:
            raise ReleaseCheckError("offline")
        return listing

    def execute(argv: list[str]) -> None:
        calls["exec"].append(argv)

    channel = ReleaseChannel(
        repo_url="https://example.test/dex", ls_remote=ls_remote, execute=execute
    )
    return channel, calls


@pytest.fixture
def template(tmp_path: Path) -> Path:
    tpl = tmp_path / "template"
    references = tpl / "skills" / "dex-run" / "references"
    references.mkdir(parents=True)
    (tpl / "skills" / "dex-run" / "SKILL.md").write_text("run skill\n")
    (references / "ingest-item.md").write_text("reference\n")
    (tpl / "skills" / "dex-query").mkdir()
    (tpl / "skills" / "dex-query" / "SKILL.md").write_text("query skill\n")
    (tpl / "dex-contract.md").write_text("contract\n")
    (tpl / "dex").write_text("#!/bin/sh\nshim\n")
    (tpl / "gitattributes").write_text("state/*.jsonl merge=union\n")
    return tpl


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    root = tmp_path / "instance"
    root.mkdir()
    return Instance(root=root)


def run(inst, channel, template, **kwargs):
    echoes: list[str] = []
    report = run_sync(
        inst,
        running_version=kwargs.pop("running_version", RUNNING),
        today=fixed_today,
        now=fixed_now,
        channel=channel,
        echo=echoes.append,
        template=template,
        **kwargs,
    )
    return report, echoes


class TestParser:
    def test_no_flags_needed(self):
        args = build_parser().parse_args([])
        assert args.previous_pin is None

    def test_previous_pin_flag(self):
        args = build_parser().parse_args(["--previous-pin", "v0.1.0"])
        assert args.previous_pin == "v0.1.0"

    def test_typoed_flags_are_loud_not_ignored(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--prev-pin", "v0.1.0"])


class TestReleaseTags:
    def test_parses_version_tags_and_collapses_peeled_refs(self):
        tags = release_tags(listing_for("v0.1.0", "v0.2.0"))
        assert dict(tags) == {"v0.1.0": (0, 1, 0), "v0.2.0": (0, 2, 0)}

    def test_non_release_tags_are_ignored(self):
        listing = listing_for("v0.1.0", "inbox", "some-experiment")
        assert dict(release_tags(listing)) == {"v0.1.0": (0, 1, 0)}

    def test_unprefixed_versions_are_releases_too(self):
        assert dict(release_tags(listing_for("0.3.1"))) == {"0.3.1": (0, 3, 1)}

    def test_empty_listing(self):
        assert release_tags("") == []


class TestBootstrap:
    def test_first_tag_aware_sync_pins_its_own_release(self, inst, template):
        channel, calls = make_channel(listing_for("v0.1.0"))
        report, _ = run(inst, channel, template)
        assert read_pin(inst.root) == "v0.1.0"
        assert calls["exec"] == []
        assert "## Sync — engine pinned at v0.1.0" in report
        assert "first tag-aware sync" in report

    def test_bootstrap_runs_migrations_and_template_sync(self, inst, template):
        channel, _ = make_channel(listing_for("v0.1.0"))
        report, _ = run(inst, channel, template)
        assert read_applied(log_path(inst.root)) == {1, 2, 3}
        assert (inst.root / "bin" / "dex").read_text() == "#!/bin/sh\nshim\n"
        assert (inst.root / ".gitattributes").exists()
        assert "### Migrations applied — 3" in report

    def test_unreleased_newer_engine_stays_unpinned(self, inst, template):
        channel, calls = make_channel(listing_for("v0.1.0"))
        report, _ = run(inst, channel, template, running_version="0.2.0")
        assert read_pin(inst.root) is None
        assert calls["exec"] == []
        assert "engine unpinned" in report
        assert "left unpinned until it is released" in report


class TestBumpAndReexec:
    def test_newer_release_bumps_pin_then_reexecs(self, inst, template):
        write_pin(inst.root, "v0.1.0")
        channel, calls = make_channel(listing_for("v0.1.0", "v0.1.2"))
        report, echoes = run(inst, channel, template)
        assert report is None  # re-exec'd; the new process renders the report
        assert read_pin(inst.root) == "v0.1.2"
        assert calls["exec"] == [
            [
                "uvx",
                "--from",
                "git+https://example.test/dex@v0.1.2",
                "dex-sync",
                "--previous-pin",
                "v0.1.0",
            ]
        ]
        assert any("v0.1.2" in line for line in echoes)

    def test_reexec_happens_before_migrations_and_template_sync(self, inst, template):
        # Steps 3-5 must run on the NEW code — nothing may touch state first.
        write_pin(inst.root, "v0.1.0")
        channel, _ = make_channel(listing_for("v0.2.0"))
        run(inst, channel, template)
        assert not log_path(inst.root).exists()
        assert not (inst.root / "bin" / "dex").exists()

    def test_tag_ordering_is_numeric_never_lexicographic(self, inst, template):
        # "0.10.0" > "0.9.1" is False as strings — the bump must compare tuples.
        write_pin(inst.root, "v0.9.1")
        channel, calls = make_channel(listing_for("v0.9.1", "v0.10.0"))
        run(inst, channel, template)
        assert read_pin(inst.root) == "v0.10.0"
        assert len(calls["exec"]) == 1

    def test_bootstrap_with_newer_release_pins_and_reexecs(self, inst, template):
        channel, calls = make_channel(listing_for("v0.1.2"))
        report, _ = run(inst, channel, template)
        assert report is None
        assert read_pin(inst.root) == "v0.1.2"
        assert calls["exec"] == [
            ["uvx", "--from", "git+https://example.test/dex@v0.1.2", "dex-sync"]
        ]

    def test_major_upgrade_announces_loudly_and_still_applies(self, inst, template):
        write_pin(inst.root, "v0.9.3")
        channel, calls = make_channel(listing_for("v0.9.3", "v1.0.0"))
        report, echoes = run(inst, channel, template)
        assert report is None
        assert read_pin(inst.root) == "v1.0.0"
        assert len(calls["exec"]) == 1
        assert any("MAJOR ENGINE UPGRADE" in line for line in echoes)

    def test_minor_bump_is_not_announced_as_major(self, inst, template):
        write_pin(inst.root, "v0.1.0")
        channel, _ = make_channel(listing_for("v0.1.1"))
        _, echoes = run(inst, channel, template)
        assert not any("MAJOR" in line for line in echoes)


class TestSteadyStateAndEdges:
    def test_pin_at_latest_proceeds_without_exec(self, inst, template):
        write_pin(inst.root, "v0.2.0")
        channel, calls = make_channel(listing_for("v0.1.0", "v0.2.0"))
        report, _ = run(inst, channel, template)
        assert calls["exec"] == []
        assert "## Sync — engine pinned at v0.2.0" in report

    def test_pin_ahead_of_latest_is_left_alone(self, inst, template):
        write_pin(inst.root, "v0.3.0")
        channel, calls = make_channel(listing_for("v0.2.0"))
        report, _ = run(inst, channel, template)
        assert calls["exec"] == []
        assert read_pin(inst.root) == "v0.3.0"
        assert "## Sync — engine pinned at v0.3.0" in report

    def test_no_releases_yet_runs_unpinned(self, inst, template):
        channel, calls = make_channel("")
        report, _ = run(inst, channel, template)
        assert read_pin(inst.root) is None
        assert calls["exec"] == []
        assert "engine unpinned" in report
        assert "no release tags on the remote yet" in report
        # Migrations + template sync still ran (pre-first-tag mode).
        assert read_applied(log_path(inst.root)) == {1, 2, 3}
        assert (inst.root / "bin" / "dex").exists()

    def test_failed_release_check_is_a_note_not_a_dead_instance(self, inst, template):
        write_pin(inst.root, "v0.1.0")
        channel, _ = make_channel(fail=True)
        report, _ = run(inst, channel, template)
        assert read_pin(inst.root) == "v0.1.0"
        assert "release check failed (offline)" in report
        assert (inst.root / "bin" / "dex").exists()

    def test_garbage_pin_is_loud(self, inst, template):
        (inst.root / PIN_FILE).write_text("not-a-tag\n")
        channel, _ = make_channel(listing_for("v0.1.0"))
        with pytest.raises(ValueError, match="not a release tag"):
            run(inst, channel, template)

    def test_previous_pin_shows_the_bump_in_the_report(self, inst, template):
        write_pin(inst.root, "v0.1.2")
        channel, _ = make_channel(listing_for("v0.1.2"))
        report, _ = run(inst, channel, template, previous_pin="v0.1.0")
        assert "## Sync — pin bumped v0.1.0 → v0.1.2" in report
        assert "MAJOR" not in report

    def test_previous_pin_across_a_major_announces_loudly_in_the_report(self, inst, template):
        # The pre-exec echo dies with the process that execvp'd away; the
        # post-re-exec report is where the major reaches the owner.
        write_pin(inst.root, "v1.0.0")
        channel, _ = make_channel(listing_for("v1.0.0"))
        report, _ = run(inst, channel, template, previous_pin="v0.9.3")
        assert "## Sync — MAJOR upgrade v0.9.3 → v1.0.0" in report
        assert "MAJOR ENGINE UPGRADE" in report


class TestMigrationsInTheFlow:
    def test_migrations_run_before_template_sync(self, inst, template):
        seen: list[bool] = []

        def migrate(root: Path):
            seen.append((root / "bin" / "dex").exists())
            return []

        channel, _ = make_channel(listing_for("v0.1.0"))
        run(inst, channel, template, migrate=migrate)
        assert seen == [False]  # state work happened before any template write

    def test_migration_reports_render_on_the_sync_report(self, inst, template):
        applied = [
            AppliedMigration(
                number=1,
                intent="renames",
                report=MigrationReport(
                    actions=["ledger: 3 line(s) translated"],
                    skipped=[Skipped(what="ledger line 7", why="unknown field(s) ['x']")],
                    anomalies=[],
                ),
            )
        ]
        channel, _ = make_channel(listing_for("v0.1.0"))
        report, _ = run(inst, channel, template, migrate=lambda _root: applied)
        assert "- **migration 1** — renames" in report
        assert "ledger: 3 line(s) translated" in report
        assert "**Repair with judgment** — 1 skipped" in report
        assert "ledger line 7" in report


class TestTemplateSync:
    def test_copies_skills_recursively_and_machinery(self, inst, template):
        changed = sync(inst.root, template=template)
        assert ".claude/skills/dex-run/SKILL.md" in changed
        assert ".claude/skills/dex-run/references/ingest-item.md" in changed
        assert ".claude/skills/dex-query/SKILL.md" in changed
        assert ".claude/dex-contract.md" in changed
        assert "bin/dex" in changed
        assert ".gitattributes" in changed
        assert (inst.root / "bin" / "dex").stat().st_mode & 0o111  # executable

    def test_second_sync_changes_nothing(self, inst, template):
        sync(inst.root, template=template)
        assert sync(inst.root, template=template) == []

    def test_sync_ensures_the_cache_directory_exists(self, inst, template):
        # A migrated pre-existing instance never went through the scaffold
        # that creates `cache/`, and the per-item procedure renders every
        # receipt through cache/receipt.json — sync makes §4's claim true.
        assert not (inst.root / "cache").exists()
        sync(inst.root, template=template)
        assert (inst.root / "cache").is_dir()
        # Idempotent, harmless on every run — and never a reported change.
        assert sync(inst.root, template=template) == []

    def test_retired_engine_skills_are_removed(self, inst, template):
        retired = inst.root / ".claude" / "skills" / "dex-ingest"
        retired.mkdir(parents=True)
        (retired / "SKILL.md").write_text("stale procedure\n")
        changed = sync(inst.root, template=template)
        assert not retired.exists()
        assert "removed .claude/skills/dex-ingest (retired engine skill)" in changed

    def test_retired_skill_symlinks_unlink_without_following(self, inst, template):
        target = inst.root / "elsewhere"
        target.mkdir()
        (target / "SKILL.md").write_text("linked content\n")
        skills = inst.root / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "dex-ingest").symlink_to(target)
        changed = sync(inst.root, template=template)
        assert not (skills / "dex-ingest").exists()
        assert (target / "SKILL.md").exists()  # the link's target is untouched
        assert "removed .claude/skills/dex-ingest (retired engine skill)" in changed

    def test_non_engine_skills_are_never_touched(self, inst, template):
        theirs = inst.root / ".claude" / "skills" / "my-own-skill"
        theirs.mkdir(parents=True)
        (theirs / "SKILL.md").write_text("instance-owned\n")
        sync(inst.root, template=template)
        assert (theirs / "SKILL.md").read_text() == "instance-owned\n"

    def test_removals_render_on_the_sync_report(self, inst, template):
        retired = inst.root / ".claude" / "skills" / "dex-ingest"
        retired.mkdir(parents=True)
        (retired / "SKILL.md").write_text("stale\n")
        channel, _ = make_channel("")
        report, _ = run(inst, channel, template)
        assert "removed .claude/skills/dex-ingest" in report

    def test_instance_owned_files_untouched(self, inst, template):
        (inst.root / "CLAUDE.md").write_text("mine\n")
        write_pin(inst.root, "v0.1.0")
        sync(inst.root, template=template)
        assert (inst.root / "CLAUDE.md").read_text() == "mine\n"
        assert read_pin(inst.root) == "v0.1.0"  # the pin is instance-owned

    def test_refreshed_files_appear_in_the_report_notes(self, inst, template):
        channel, _ = make_channel("")
        report, _ = run(inst, channel, template)
        assert "refreshed: bin/dex" in report
        assert "**Machinery changes** — 6" in report


class TestMain:
    def test_main_renders_the_report_from_cwd(self, inst, template, monkeypatch, capsys):
        channel, _ = make_channel("")
        monkeypatch.setattr("dex_engine.sync.default_channel", lambda: channel)
        monkeypatch.setattr("dex_engine.sync._bundled_template", lambda: template)
        monkeypatch.chdir(inst.root)
        main([])
        out = capsys.readouterr().out
        assert "## Sync — engine unpinned" in out
        assert "### Migrations applied — 3" in out

    def test_main_is_loud_on_a_garbage_pin(self, inst, template, monkeypatch):
        (inst.root / PIN_FILE).write_text("garbage\n")
        channel, _ = make_channel(listing_for("v0.1.0"))
        monkeypatch.setattr("dex_engine.sync.default_channel", lambda: channel)
        monkeypatch.setattr("dex_engine.sync._bundled_template", lambda: template)
        monkeypatch.chdir(inst.root)
        with pytest.raises(SystemExit, match=r"dex-sync: .*not a release tag"):
            main([])
