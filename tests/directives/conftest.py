"""Fixture directives: built in memory, or written as a throwaway package.

The framework's tests never exercise the engine's own set, so a directive
the engine ships later changes nothing there; each shipped directive has a
test file of its own.
"""

import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from dex_engine.directives import Directive


def make_directive(number: int, *, unmet: Sequence[str] = (), intent: str = "") -> Directive:
    """A directive whose check reports ``unmet``, whatever the instance holds."""
    return Directive(
        number=number,
        intent=intent or f"fixture directive {number}",
        instructions=f"# Directive {number}\n\nDo the fixture work for {number}.\n",
        check=lambda _root: list(unmet),
    )


def directive_module(intent: str, unmet: Sequence[str] = ()) -> str:
    """The source of a directive module defining ``INTENT`` and a fixed ``check``."""
    return f"INTENT = {intent!r}\n\n\ndef check(root):\n    return {list(unmet)!r}\n"


@pytest.fixture
def fixture_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[dict[str, str]], str]:
    """Write a package holding ``files`` and return its importable name.

    Every package gets a fresh name, so a module one test imported can
    never answer for another test's file of the same name.
    """

    def write(files: dict[str, str]) -> str:
        name = f"fixture_set_{uuid.uuid4().hex}"
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for filename, text in files.items():
            (package / filename).write_text(text, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        return name

    return write
