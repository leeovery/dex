"""Tests for serve/connect.py: the merge into a live config file, and its refusals.

The config belongs to another application, so every case checks the file as
much as the entry: what else was in it survives a write, and a file this
command cannot read is left exactly as it was found.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from dex_engine.serve.connect import SERVER, connect, default_config_path, main, server_entry

from .conftest import BOOKS, COFFEE

# A config with an unrelated server and unrelated app preferences: the shape a
# fresh install grows into, and everything the merge must not touch.
OTHER = {
    "command": "/opt/other/bin/other-server",
    "args": ["--flag"],
}
CONFIG = {
    "mcpServers": {"other": OTHER},
    "globalShortcut": "",
    "theme": "dark",
    "locale": "français",
}


@pytest.fixture(autouse=True)
def _nowhere_near_the_real_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at the tmp tree for every test in this file.

    ``default_config_path`` resolves through ``Path.home()``, and a test that
    forgets ``--config`` on a mac would otherwise write into the developer's
    own live desktop config — which happened once, mid-development.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def anchored(roots: list[Path]) -> list[Path]:
    """The two instance roots, each carrying the shim a client would run."""
    for root in roots:
        shim = root / "bin" / "dex"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n", encoding="utf-8")
    return roots


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def written(config: Path) -> dict:
    return json.loads(config.read_text(encoding="utf-8"))


def entry(config: Path) -> dict:
    return written(config)["mcpServers"][SERVER]


class TestEntry:
    def test_the_command_is_the_anchor_shim(self, anchored):
        built = server_entry(anchor=anchored[0], roots=anchored, path="/bin")
        assert built["command"] == str(anchored[0] / "bin" / "dex")

    def test_every_root_is_its_own_flag(self, anchored):
        built = server_entry(anchor=anchored[0], roots=anchored, path="/bin")
        assert built["args"] == [
            "serve",
            "--instance",
            str(anchored[0]),
            "--instance",
            str(anchored[1]),
        ]

    def test_the_path_travels_in_the_environment(self, anchored):
        built = server_entry(anchor=anchored[0], roots=anchored, path="/opt/homebrew/bin:/usr/bin")
        assert built["env"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}


class TestMerge:
    def test_the_entry_lands_under_its_name(self, config, anchored):
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert entry(config)["command"] == str(anchored[0] / "bin" / "dex")

    def test_everything_else_survives(self, config, anchored):
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        after = written(config)
        assert after["mcpServers"]["other"] == OTHER
        assert {key: after[key] for key in ("globalShortcut", "theme", "locale")} == {
            key: CONFIG[key] for key in ("globalShortcut", "theme", "locale")
        }

    def test_the_file_keeps_its_formatting(self, config, anchored):
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        text = config.read_text(encoding="utf-8")
        assert '\n  "mcpServers": {' in text
        assert '\n    "other": {' in text
        # Not ç: the app writes UTF-8, and a re-serialized file that
        # escaped its non-ASCII would be a diff the owner never asked for.
        assert '"locale": "français"' in text
        assert text.endswith("}\n")

    def test_an_existing_entry_is_replaced_in_place(self, config, anchored):
        stale = {"mcpServers": {SERVER: {"command": "/gone/bin/dex"}, "other": OTHER}}
        config.write_text(json.dumps(stale, indent=2), encoding="utf-8")
        connect(config, anchor=anchored[1], roots=[anchored[1]], path="/bin")
        servers = written(config)["mcpServers"]
        assert servers[SERVER]["command"] == str(anchored[1] / "bin" / "dex")
        assert list(servers) == [SERVER, "other"]

    def test_a_config_with_no_servers_yet_grows_the_key(self, tmp_path, anchored):
        config = tmp_path / "config.json"
        config.write_text('{"theme": "dark"}\n', encoding="utf-8")
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert list(written(config)) == ["theme", "mcpServers"]

    def test_the_summary_names_what_changed_and_what_to_do(self, config, anchored):
        summary = connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert summary.startswith(f"added the '{SERVER}' server in {config}")
        assert COFFEE in summary
        assert BOOKS in summary
        assert "relaunch" in summary

    def test_a_replacement_says_so(self, config, anchored):
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert connect(config, anchor=anchored[0], roots=anchored, path="/bin").startswith(
            "replaced"
        )


class TestRefusals:
    def test_a_missing_file_is_never_created(self, tmp_path, anchored):
        config = tmp_path / "absent.json"
        with pytest.raises(ValueError, match="no such file") as excinfo:
            connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert str(config) in str(excinfo.value)
        assert "launch it once" in str(excinfo.value)
        assert not config.exists()

    def test_unparseable_json_leaves_the_file_alone(self, tmp_path, anchored):
        config = tmp_path / "config.json"
        config.write_text('{"mcpServers": {,}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON") as excinfo:
            connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert str(config) in str(excinfo.value)
        assert config.read_text(encoding="utf-8") == '{"mcpServers": {,}\n'

    def test_a_config_that_is_not_an_object_is_refused(self, tmp_path, anchored):
        config = tmp_path / "config.json"
        config.write_text("[]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected a JSON object") as excinfo:
            connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        # Naming what was found is the whole diagnostic: the owner is being
        # sent to a file this command will not touch.
        assert "got list" in str(excinfo.value)
        assert config.read_text(encoding="utf-8") == "[]\n"

    def test_servers_that_are_not_an_object_are_refused(self, tmp_path, anchored):
        config = tmp_path / "config.json"
        config.write_text('{"mcpServers": []}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="'mcpServers' is not a JSON object"):
            connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert config.read_text(encoding="utf-8") == '{"mcpServers": []}\n'


class TestDefaultConfigPath:
    def test_macos_has_a_verified_location(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_config_path() == (
            tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
        )

    def test_anywhere_else_asks_for_the_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(ValueError, match="--config") as excinfo:
            default_config_path()
        assert "linux" in str(excinfo.value)


class TestMain:
    def test_the_anchor_defaults_to_the_first_instance(self, config, anchored, capsys):
        main(
            [
                "--instance",
                str(anchored[0]),
                "--instance",
                str(anchored[1]),
                "--config",
                str(config),
            ]
        )
        assert entry(config)["command"] == str(anchored[0] / "bin" / "dex")
        out = capsys.readouterr().out
        assert out.startswith("added")
        assert out.endswith("\n")

    def test_an_explicit_anchor_wins(self, config, anchored):
        main(
            [
                "--instance",
                str(anchored[0]),
                "--anchor",
                str(anchored[1]),
                "--config",
                str(config),
            ]
        )
        assert entry(config)["command"] == str(anchored[1] / "bin" / "dex")
        assert entry(config)["args"] == ["serve", "--instance", str(anchored[0])]

    def test_the_installing_shell_s_path_is_captured(self, config, anchored, monkeypatch):
        monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
        main(["--instance", str(anchored[0]), "--config", str(config)])
        assert entry(config)["env"] == {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}

    def test_a_shell_with_no_path_still_writes_a_usable_one(self, config, anchored, monkeypatch):
        # Vanishingly rare, but the entry is JSON: a missing PATH written as
        # null is a server the app spawns with no PATH at all, which is worse
        # than the bare default it would otherwise have got.
        monkeypatch.delenv("PATH", raising=False)
        main(["--instance", str(anchored[0]), "--config", str(config)])
        assert entry(config)["env"] == {"PATH": os.defpath}

    def test_relative_roots_are_resolved(self, config, anchored, monkeypatch):
        monkeypatch.chdir(anchored[0].parent)
        main(["--instance", COFFEE, "--config", str(config)])
        assert entry(config)["args"] == ["serve", "--instance", str(anchored[0])]

    def test_a_root_that_is_not_an_instance_exits_cleanly(self, config, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(tmp_path), "--config", str(config)])
        assert "dex-connect" in str(excinfo.value)
        assert "not a dex instance" in str(excinfo.value)

    def test_a_duplicate_name_exits_before_writing(self, config, anchored, tmp_path):
        clone = tmp_path / "elsewhere" / COFFEE
        (clone / "corpus").mkdir(parents=True)
        (clone / "state").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--instance",
                    str(anchored[0]),
                    "--instance",
                    str(clone),
                    "--config",
                    str(config),
                ]
            )
        assert COFFEE in str(excinfo.value)
        assert SERVER not in written(config)["mcpServers"]

    def test_an_anchor_without_a_shim_exits_cleanly(self, config, roots):
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(roots[0]), "--config", str(config)])
        assert "no bin/dex" in str(excinfo.value)
        assert SERVER not in written(config)["mcpServers"]

    def test_an_anchor_that_is_not_an_instance_exits_cleanly(self, config, anchored, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--instance",
                    str(anchored[0]),
                    "--anchor",
                    str(tmp_path / "absent"),
                    "--config",
                    str(config),
                ]
            )
        assert "no such directory" in str(excinfo.value)

    def test_no_instance_is_a_usage_error(self, config):
        with pytest.raises(SystemExit):
            main(["--config", str(config)])

    def test_off_macos_the_missing_flag_is_named(self, anchored, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(anchored[0])])
        assert "--config" in str(excinfo.value)

    def test_on_macos_the_default_location_is_used(self, tmp_path, anchored, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("HOME", str(tmp_path))
        config = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        main(["--instance", str(anchored[0])])
        assert entry(config)["command"] == str(anchored[0] / "bin" / "dex")
