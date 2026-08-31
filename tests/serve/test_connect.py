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

from dex_engine.serve.connect import (
    CLIENTS,
    SERVER,
    absence,
    code_config_path,
    connect,
    connect_code,
    connect_gaps,
    default_config_path,
    detect,
    main,
    server_entry,
)

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
    """Put every client's config, and the CLI that writes one, inside the tmp tree.

    ``default_config_path`` resolves through ``Path.home()``, and a test that
    forgets ``--config`` on a mac would otherwise write into the developer's
    own live desktop config — which happened once, mid-development.
    ``CLAUDE_CONFIG_DIR`` is the same wall for Claude Code, and an empty PATH
    is the third: with no ``claude`` on it, ``detect`` says the client is not
    here, so a developer who has Claude Code installed does not get a second
    client written by every test that never mentioned one.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


# What the real CLI does, including the two behaviours the write order exists
# for, both verified against claude on 2026-08-31: `add` REFUSES a name that
# already exists rather than replacing it, and `remove` of a name that is not
# there is itself an error. A stub that quietly replaced, or that removed
# absent names happily, would let both bugs through.
FAKE_CLAUDE = r"""\
import json, os, sys

argv = sys.argv[1:]
cfg = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
with open(os.environ["CLAUDE_LOG"], "a") as log:
    log.write(" ".join(argv) + "\n")
data = json.load(open(cfg)) if os.path.exists(cfg) else {}
servers = data.setdefault("mcpServers", {})
name = argv[4] if len(argv) > 4 else ""
if argv[:2] == ["mcp", "remove"]:
    if name not in servers:
        print(f'No MCP server named "{name}" in user scope', file=sys.stderr)
        sys.exit(1)
    del servers[name]
elif argv[:2] == ["mcp", "add"]:
    if name in servers:
        print(f"MCP server {name} already exists in user config", file=sys.stderr)
        sys.exit(1)
    rest = argv[argv.index("--") + 1 :]
    servers[name] = {"type": "stdio", "command": rest[0], "args": rest[1:]}
else:
    print("unsupported", file=sys.stderr)
    sys.exit(2)
with open(cfg, "w") as out:
    json.dump(data, out)
"""


@pytest.fixture
def claude_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `claude` on PATH that behaves like the real one, and logs its argv."""
    binary = tmp_path / "empty-bin" / "claude"
    # An absolute interpreter, not `env python3`: PATH is pinned to this
    # directory, so nothing else on it can be found.
    binary.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    binary.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("CLAUDE_LOG", str(log))
    return log


def calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def code_servers() -> dict:
    path = code_config_path()
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"] if path.exists() else {}


def why(client: str, config: Path | None = None) -> str:
    """The reason a client is out of reach — asserted present, so it reads as one."""
    reason = absence(client, config=config)
    assert reason is not None
    return reason


def desktop_gap(root: Path, config: Path) -> str | None:
    """The desktop client's gap alone — what this file asserted before there were two."""
    return next(
        (row["gap"] for row in connect_gaps(root, config=config) if row["client"] == "desktop"),
        None,
    )


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
        assert summary.startswith(f"desktop app: added the '{SERVER}' server in {config}")
        assert COFFEE in summary
        assert BOOKS in summary
        assert "relaunch" in summary

    def test_a_replacement_says_so(self, config, anchored):
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert connect(config, anchor=anchored[0], roots=anchored, path="/bin").startswith(
            "desktop app: replaced"
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


class TestConnectGap:
    """The read-only look sync takes: is this instance reachable from chat?

    Silence is the answer everywhere the question does not apply — a Linux
    box, a cloud runner, a machine with no desktop app — so most of these
    assert that nothing is said rather than what is said.
    """

    def test_a_config_with_no_dex_server_is_unconnected(self, config, roots):
        assert desktop_gap(roots[0], config) == "unconnected"

    def test_a_config_that_never_grew_the_key_is_unconnected(self, tmp_path, roots):
        config = tmp_path / "config.json"
        config.write_text('{"theme": "dark"}\n', encoding="utf-8")
        assert desktop_gap(roots[0], config) == "unconnected"

    def test_what_connect_writes_closes_the_gap(self, config, anchored):
        # The reader reads back exactly what the writer wrote — every root
        # it served, not only the anchor.
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        assert desktop_gap(anchored[0], config) is None
        assert desktop_gap(anchored[1], config) is None

    def test_an_instance_the_entry_does_not_serve_is_unlisted(self, config, anchored):
        connect(config, anchor=anchored[0], roots=[anchored[0]], path="/bin")
        assert desktop_gap(anchored[1], config) == "unlisted"

    def test_the_same_directory_spelled_two_ways_is_the_same_instance(
        self, config, anchored, tmp_path
    ):
        # macOS hands out /tmp as a symlink to /private/tmp, so the root a
        # command runs in and the resolved root the entry holds are the same
        # directory under two names.
        connect(config, anchor=anchored[0], roots=anchored, path="/bin")
        link = tmp_path / "linked"
        link.symlink_to(anchored[0])
        assert desktop_gap(link, config) is None

    def test_only_what_follows_an_instance_flag_counts_as_served(self, tmp_path, roots):
        # A path is a served root because `--instance` introduced it, not
        # because it appears in the list: another flag's value naming this
        # root is not this root being served.
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps({"mcpServers": {SERVER: {"args": ["serve", "--elsewhere", str(roots[0])]}}}),
            encoding="utf-8",
        )
        assert desktop_gap(roots[0], config) == "unlisted"

    def test_an_entry_whose_args_cannot_be_read_serves_nothing(self, tmp_path, roots):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"mcpServers": {SERVER: {"args": "serve"}}}), encoding="utf-8")
        assert desktop_gap(roots[0], config) == "unlisted"

    def test_a_missing_config_says_nothing(self, tmp_path, roots):
        assert desktop_gap(roots[0], tmp_path / "absent.json") is None

    def test_an_unparseable_config_says_nothing(self, tmp_path, roots):
        config = tmp_path / "config.json"
        config.write_text('{"mcpServers": {,}\n', encoding="utf-8")
        assert desktop_gap(roots[0], config) is None

    def test_a_config_that_is_not_an_object_says_nothing(self, tmp_path, roots):
        config = tmp_path / "config.json"
        config.write_text("[]\n", encoding="utf-8")
        assert desktop_gap(roots[0], config) is None

    def test_servers_that_are_not_an_object_say_nothing(self, tmp_path, roots):
        # A config `connect` itself would refuse: its diagnosis belongs to
        # the command someone asked for, not to a sync report.
        config = tmp_path / "config.json"
        config.write_text('{"mcpServers": []}\n', encoding="utf-8")
        assert desktop_gap(roots[0], config) is None

    def test_off_macos_no_location_is_guessed_at(self, monkeypatch, roots):
        monkeypatch.setattr(sys, "platform", "linux")
        assert connect_gaps(roots[0]) == []

    def test_on_macos_the_default_location_is_read(self, tmp_path, monkeypatch, roots):
        monkeypatch.setattr(sys, "platform", "darwin")
        config = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        assert connect_gaps(roots[0]) == [{"client": "desktop", "gap": "unconnected"}]


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
        assert out.startswith("desktop app: added")
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


class TestDetect:
    """Three ways a client is out of reach, kept three answers."""

    def test_no_claude_on_path_is_not_installed(self):
        assert detect("code") is False
        assert "not installed here" in why("code")

    @pytest.mark.usefixtures("claude_cli")
    def test_a_claude_on_path_is_installed(self):
        assert detect("code") is True
        assert absence("code") is None

    def test_a_desktop_config_that_exists_is_installed(self, config):
        assert detect("desktop", config=config) is True

    def test_a_desktop_that_never_launched_is_named_by_its_file(self, tmp_path):
        absent = tmp_path / "absent.json"
        assert detect("desktop", config=absent) is False
        assert "launch it once" in why("desktop", absent)

    def test_off_macos_the_reason_is_the_missing_flag(self, monkeypatch):
        # Not "the app isn't here" — the app may well be, and --config is the
        # only thing that would find it. Collapsing the two loses the fix.
        monkeypatch.setattr(sys, "platform", "linux")
        reason = why("desktop")
        assert "--config" in reason
        assert "linux" in reason


class TestCodeWrite:
    """The Claude Code client: written through its own CLI, never by merging."""

    @pytest.mark.usefixtures("claude_cli")
    def test_the_entry_is_the_same_launcher(self, anchored):
        connect_code(anchor=anchored[0], roots=anchored)
        written_entry = code_servers()[SERVER]
        assert written_entry["command"] == str(anchored[0] / "bin" / "dex")
        assert written_entry["args"] == [
            "serve",
            "--instance",
            str(anchored[0]),
            "--instance",
            str(anchored[1]),
        ]

    @pytest.mark.usefixtures("claude_cli")
    def test_no_path_is_frozen_into_the_entry(self, anchored):
        # Claude Code inherits a real user PATH. A snapshot of install-day
        # PATH would be a downgrade, not insurance — unlike the desktop app,
        # whose GUI-spawned child gets launchd's bare PATH and needs one.
        connect_code(anchor=anchored[0], roots=anchored)
        assert "env" not in code_servers()[SERVER]
        assert "env" not in server_entry(anchor=anchored[0], roots=anchored, path=None)

    def test_a_first_connect_survives_the_clearing_remove(self, claude_cli, anchored):
        # Removing a name that is not there is an error to that CLI and the
        # intended state to this one, so the first connect on a machine must
        # not fail on it. Found by running the real thing.
        connect_code(anchor=anchored[0], roots=anchored)
        assert code_servers()[SERVER]["command"] == str(anchored[0] / "bin" / "dex")
        assert calls(claude_cli)[0] == f"mcp remove -s user {SERVER}"

    def test_it_removes_before_it_adds(self, claude_cli, anchored):
        connect_code(anchor=anchored[0], roots=anchored)
        assert calls(claude_cli) == [
            f"mcp remove -s user {SERVER}",
            f"mcp add -s user {SERVER} -- "
            + " ".join([str(anchored[0] / "bin" / "dex"), "serve"])
            + f" --instance {anchored[0]} --instance {anchored[1]}",
        ]

    @pytest.mark.usefixtures("claude_cli")
    def test_a_changed_roster_actually_replaces(self, anchored):
        # The whole reason for the remove: `claude mcp add` REFUSES a name
        # that already exists rather than replacing it, and re-running with a
        # different list is the documented way the served set changes.
        connect_code(anchor=anchored[0], roots=anchored)
        connect_code(anchor=anchored[0], roots=[anchored[0]])
        assert code_servers()[SERVER]["args"] == ["serve", "--instance", str(anchored[0])]

    @pytest.mark.usefixtures("claude_cli")
    def test_the_summary_says_what_changed_and_what_to_do(self, anchored):
        first = connect_code(anchor=anchored[0], roots=anchored)
        assert first.startswith(f"Claude Code: added the '{SERVER}' server")
        assert COFFEE in first
        assert BOOKS in first
        assert "restart any that are open" in first
        assert connect_code(anchor=anchored[0], roots=anchored).startswith("Claude Code: replaced")

    def test_a_cli_that_refuses_is_reported_in_its_own_words(self, tmp_path, anchored):
        binary = tmp_path / "empty-bin" / "claude"
        binary.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n", encoding="utf-8")
        binary.chmod(0o755)
        with pytest.raises(ValueError, match="not logged in"):
            connect_code(anchor=anchored[0], roots=anchored)

    def test_without_the_cli_nothing_is_attempted(self, anchored):
        with pytest.raises(ValueError, match="not on PATH"):
            connect_code(anchor=anchored[0], roots=anchored)


class TestClientSelection:
    """Which clients one run writes, and what it says about the ones it skips."""

    @pytest.mark.usefixtures("claude_cli")
    def test_by_default_every_client_here_is_written(self, config, anchored, capsys):
        main(["--instance", str(anchored[0]), "--config", str(config)])
        assert entry(config)["command"] == str(anchored[0] / "bin" / "dex")
        assert SERVER in code_servers()
        out = capsys.readouterr().out
        assert "desktop app: added" in out
        assert "Claude Code: added" in out

    @pytest.mark.usefixtures("claude_cli")
    def test_one_named_client_is_the_only_one_written(self, config, anchored):
        main(["--instance", str(anchored[0]), "--client", "code", "--config", str(config)])
        assert SERVER in code_servers()
        assert SERVER not in written(config)["mcpServers"]

    @pytest.mark.usefixtures("claude_cli")
    def test_a_declined_client_can_be_left_out(self, config, anchored):
        main(["--instance", str(anchored[0]), "--client", "desktop", "--config", str(config)])
        assert SERVER in written(config)["mcpServers"]
        assert code_servers() == {}

    def test_a_client_that_is_not_here_is_reported_not_guessed_at(self, config, anchored, capsys):
        main(["--instance", str(anchored[0]), "--config", str(config)])
        out = capsys.readouterr().out
        assert "desktop app: added" in out
        assert "code: skipped — no claude CLI on PATH" in out

    def test_a_run_that_reached_nothing_fails_loudly(self, tmp_path, anchored):
        # Two skips and a zero exit is how an owner ends up believing they
        # are connected when nothing was written at all.
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(anchored[0]), "--config", str(tmp_path / "absent.json")])
        assert "nothing was written" in str(excinfo.value)
        assert "launch it once" in str(excinfo.value)

    def test_an_unknown_client_is_a_usage_error(self, config, anchored):
        with pytest.raises(SystemExit):
            main(["--instance", str(anchored[0]), "--client", "cowork", "--config", str(config)])

    def test_the_same_client_twice_is_written_once(self, config, claude_cli, anchored):
        main(
            [
                "--instance",
                str(anchored[0]),
                "--client",
                "code",
                "--client",
                "code",
                "--config",
                str(config),
            ]
        )
        assert calls(claude_cli) == [
            f"mcp remove -s user {SERVER}",
            (
                f"mcp add -s user {SERVER} -- {anchored[0] / 'bin' / 'dex'} serve "
                f"--instance {anchored[0]}"
            ),
        ]


class TestPerClientGaps:
    """Sync's read: which surface is missing, not one bit for two clients."""

    @pytest.mark.usefixtures("claude_cli")
    def test_each_client_is_answered_for_separately(self, config, anchored):
        connect_code(anchor=anchored[0], roots=anchored)
        # Wired into Claude Code, not into the app — the exact state that
        # went unreported when this check read one config.
        assert connect_gaps(anchored[0], config=config) == [
            {"client": "desktop", "gap": "unconnected"}
        ]

    @pytest.mark.usefixtures("claude_cli")
    def test_both_gaps_are_reported_together(self, config, anchored):
        assert connect_gaps(anchored[0], config=config) == [
            {"client": "desktop", "gap": "unconnected"},
            {"client": "code", "gap": "unconnected"},
        ]

    def test_a_client_that_is_not_here_contributes_nothing(self, config, anchored):
        assert [row["client"] for row in connect_gaps(anchored[0], config=config)] == ["desktop"]

    @pytest.mark.usefixtures("claude_cli")
    def test_an_absent_client_does_not_end_the_search(self, tmp_path, anchored):
        # A machine with no desktop app but with Claude Code: skipping the
        # first client must not stop the loop before it reaches the second.
        # Found by mutation audit — `continue` to `break` survived.
        assert connect_gaps(anchored[0], config=tmp_path / "absent.json") == [
            {"client": "code", "gap": "unconnected"}
        ]

    @pytest.mark.usefixtures("claude_cli")
    def test_writing_both_closes_both(self, config, anchored):
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
        assert connect_gaps(anchored[0], config=config) == []
        assert connect_gaps(anchored[1], config=config) == []

    @pytest.mark.usefixtures("claude_cli")
    def test_an_anchor_that_left_the_machine_is_broken_not_connected(self, config, anchored):
        # Listing a root is not the same as being able to launch it: the
        # shim is checked at write time and never again, so a moved or
        # deleted anchor fails inside a process that reports nothing back.
        main(["--instance", str(anchored[0]), "--config", str(config)])
        (anchored[0] / "bin" / "dex").unlink()
        assert connect_gaps(anchored[0], config=config) == [
            {"client": "desktop", "gap": "broken"},
            {"client": "code", "gap": "broken"},
        ]

    def test_every_client_named_here_is_one_the_report_can_render(self):
        # The surface fails a client it does not know, and this list is what
        # decides which ones reach it.
        assert set(CLIENTS) == {"desktop", "code"}
