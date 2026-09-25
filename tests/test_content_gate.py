"""The content gate: inbox, normalize, enrich and exclude refuse while a directive is pending.

Every gated invocation runs through its real ``main`` against an instance
where each would write (a capture to turn into an item, an item to exclude,
payloads to apply, an export to normalize), so a refusal that let anything
through would show as a changed tree.
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from dex_engine import directive, enrich, exclude, inbox, instance_map, lint, normalize
from dex_engine.directives import (
    Directive,
    DirectivesPendingError,
    append_done,
    log_path,
    refuse_while_pending,
)
from dex_engine.pipeline.types import Instance
from tests.directives.conftest import make_directive
from tests.test_exclude import ITEM, write_item_stub
from tests.test_normalize import message, write_export

SHIPPED = [make_directive(1)]
TODAY = datetime.date(2026, 9, 24)
CAPTURE = "inbox/20260818-101530.md"
REACHED = "reached the command's own work"


@dataclass(frozen=True)
class Gated:
    """One gated invocation, and the function its main hands the work to past the gate."""

    prog: str
    main: Callable[[list[str] | None], None]
    argv: list[str]
    worker: str


def invocation(gated: Gated) -> object:
    return pytest.param(gated, id=" ".join([gated.prog, *gated.argv]))


ENRICH_ARGVS = [
    ["run"],
    ["status"],
    ["transcribe"],
    ["fetch", ITEM, "https://example.test/more"],
    ["compact"],
    ["mark", "https://example.test/post", "dead"],
    ["pass", ITEM, "--stage", "harvest"],
    ["place", "--file", "cache/placement.json"],
    ["item", "new", CAPTURE],
    ["item", "digest", "--file", "cache/digest.json"],
    ["item", "describe", ITEM, "--of", "media-0.png", "--file", "cache/description.md"],
]

# Every gated command, each subcommand once.
GATED = [
    invocation(Gated("dex-inbox", inbox.main, [], "dex_engine.inbox.reconcile")),
    invocation(Gated("dex-inbox", inbox.main, ["ensure"], "dex_engine.inbox.ensure")),
    invocation(Gated("dex-normalize", normalize.main, [], "dex_engine.normalize.run_normalize")),
    invocation(
        Gated(
            "dex-exclude", exclude.main, ["cache/exclusions.json"], "dex_engine.exclude.run_exclude"
        )
    ),
    *(
        invocation(Gated("dex-enrich", enrich.main, argv, "dex_engine.enrich._dispatch"))
        for argv in ENRICH_ARGVS
    ),
]


def use(monkeypatch: pytest.MonkeyPatch, instance: Instance, shipped: list[Directive]) -> None:
    """Run from the instance root, with ``shipped`` as the engine's directive set."""
    for module in ("dex_engine.directives", "dex_engine.directive"):
        monkeypatch.setattr(f"{module}.discover", lambda: shipped)
    monkeypatch.chdir(instance.root)


def furnish(instance: Instance) -> None:
    """Give every gated command something it would write, were it let through."""
    capture = instance.root / CAPTURE
    capture.parent.mkdir()
    capture.write_text("https://example.test/post\n\nwhy I saved it\n")
    write_item_stub(instance, urls=("https://example.test/post",))
    write_export(instance, [message("m1", "https://example.test/chat")])
    (instance.enrichment_dir / ITEM).mkdir()
    (instance.enrichment_dir / ITEM / "media-0.png").write_bytes(b"png")
    payloads = {
        "exclusions.json": [{"id": ITEM, "reason": "meme"}],
        "placement.json": {
            "topics": [{"name": "brewing", "description": "Brew technique."}],
            "place": [{"id": ITEM, "topics": ["brewing"]}],
        },
        "digest.json": {"id": ITEM, "signal": "high", "topics": ["brewing"], "facts": ["a."]},
    }
    for name, payload in payloads.items():
        (instance.cache_dir / name).write_text(json.dumps(payload))
    (instance.cache_dir / "description.md").write_text("A chart.\n")


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def gate_line(root: Path) -> str:
    with pytest.raises(DirectivesPendingError) as refused:
        refuse_while_pending(root, SHIPPED)
    return str(refused.value)


def outcome(main: Callable[[list[str] | None], None], argv: list[str]) -> str | int | None:
    """What ``main`` exited with: its stated line or code, or ``None`` for a clean return."""
    try:
        main(argv)
    except SystemExit as exited:
        return exited.code
    return None


@pytest.mark.parametrize("gated", GATED)
class TestGatedCommands:
    def test_a_pending_directive_refuses_with_the_next_step_and_writes_nothing(
        self, gated, instance, monkeypatch
    ):
        furnish(instance)
        use(monkeypatch, instance, SHIPPED)
        before = tree(instance.root)
        assert outcome(gated.main, gated.argv) == f"{gated.prog}: {gate_line(instance.root)}"
        assert tree(instance.root) == before

    def test_with_none_pending_the_command_reaches_its_own_work(self, gated, instance, monkeypatch):
        def reached(*_args: object, **_kwargs: object) -> None:
            raise OSError(REACHED)

        furnish(instance)
        append_done(log_path(instance.root), number=1, engine="0.1.0", date=TODAY)
        use(monkeypatch, instance, SHIPPED)
        monkeypatch.setattr(gated.worker, reached)
        assert outcome(gated.main, gated.argv) == f"{gated.prog}: {REACHED}"

    def test_a_corrupt_directives_log_is_a_stated_line_not_a_traceback(
        self, gated, instance, monkeypatch
    ):
        log_path(instance.root).write_text("{torn\n")
        use(monkeypatch, instance, SHIPPED)
        exited = outcome(gated.main, gated.argv)
        assert isinstance(exited, str)
        assert exited.startswith(f"{gated.prog}: ")
        assert "directives.jsonl:1" in exited


class TestUngatedCommands:
    """Every other command runs as usual while a directive is pending."""

    @pytest.fixture(autouse=True)
    def pending(self, instance, monkeypatch):
        use(monkeypatch, instance, SHIPPED)

    def test_lint_runs(self, capsys):
        outcome(lint.main, [])
        assert capsys.readouterr().out.startswith("## ")

    def test_map_compiles(self, instance, capsys):
        assert outcome(instance_map.main, []) is None
        assert capsys.readouterr().out.startswith("map compiled:")
        assert instance.map_path.exists()

    def test_directive_list_names_what_is_pending(self, capsys):
        assert outcome(directive.main, ["list"]) is None
        assert "directive 1: fixture directive 1" in capsys.readouterr().out
