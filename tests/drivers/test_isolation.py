"""The isolation rule: a driver never imports another driver, no exceptions.

Drivers are dumb and isolated — anything two of them share is a lib beside
``transport.py`` and ``gh.py``, not one driver reaching into the other's
module. The rule is checked over the source rather than trusted to review:
it was broken once by a helper that looked too small to move, and once more
by a whole-driver delegation (``paper`` → ``web``) that stood as a blessed
exception until the article-fetch seam was hoisted into
``drivers/article.py`` and the exception went.
"""

import ast
from pathlib import Path

DRIVERS_DIR = Path(__file__).parent.parent.parent / "src" / "dex_engine" / "drivers"


def driver_modules() -> dict[str, ast.Module]:
    """Every module in the drivers package that defines a driver class."""
    modules = {}
    for path in sorted(DRIVERS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Driver") for node in tree.body
        ):
            modules[path.stem] = tree
    return modules


def imported_siblings(tree: ast.Module) -> set[str]:
    """The names of sibling driver-package modules this module imports."""
    package = "dex_engine.drivers"
    siblings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                siblings.add(node.module.split(".")[0])
            elif node.module and node.module.startswith(f"{package}."):
                siblings.add(node.module[len(package) + 1 :].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{package}."):
                    siblings.add(alias.name[len(package) + 1 :].split(".")[0])
    return siblings


class TestDriversAreIsolated:
    def test_the_package_holds_drivers_at_all(self):
        # The scan is only meaningful if it finds the drivers it walks.
        assert {"web", "podcast", "youtube", "x", "github", "file", "paper"} <= set(
            driver_modules()
        )

    def test_no_driver_imports_another_driver(self):
        drivers = driver_modules()
        offences = sorted(
            (name, sibling)
            for name, tree in drivers.items()
            for sibling in imported_siblings(tree)
            if sibling in drivers
        )
        assert offences == []
