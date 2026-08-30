"""Tests for template.py: where the bundled instance template is looked for.

The wheel's own layout is the contract — pyproject force-includes the tree's
`instance/` at `dex_engine/instance`, and a command that looked anywhere else
would refresh an instance's machinery from nothing.
"""

from pathlib import Path

from dex_engine.template import bundled_template


class TestBundledTemplate:
    def test_is_the_instance_tree_inside_the_installed_package(self):
        found = Path(str(bundled_template()))
        assert found.name == "instance"
        assert found.parent.name == "dex_engine"
