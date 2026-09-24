"""The wheel carries a directive's instructions beside its module.

Discovery reads ``directive_<n>.md`` through importlib.resources from the
installed package, so a build that dropped the markdown would ship every
directive broken while the source tree read fine. The build here is the
real one, from this repo's own build configuration, over a copy of the tree
holding one fixture directive; offline, because the build backend is in
uv's cache from the environment's own install.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

from tests.directives.conftest import directive_module

ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS = "# Fixture\n\nPerform the fixture work, then record it.\n"


def test_instructions_ship_in_the_built_wheel(tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "instance", tree / "instance")
    for name in ("pyproject.toml", "LICENSE", ".gitignore"):
        shutil.copy2(ROOT / name, tree / name)
    package = tree / "src" / "dex_engine" / "directives"
    (package / "directive_1.py").write_text(directive_module("fixture"), encoding="utf-8")
    (package / "directive_1.md").write_text(INSTRUCTIONS, encoding="utf-8")

    uv = shutil.which("uv")
    assert uv is not None, "the build needs uv on PATH, as every gate does"
    subprocess.run(  # noqa: S603 — a fixed argv over a tmp tree
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--quiet",
            "--out-dir",
            str(tmp_path / "dist"),
            str(tree),
        ],
        check=True,
        capture_output=True,
    )

    [wheel] = (tmp_path / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as built:
        assert "dex_engine/directives/directive_1.py" in built.namelist()
        assert built.read("dex_engine/directives/directive_1.md").decode() == INSTRUCTIONS
