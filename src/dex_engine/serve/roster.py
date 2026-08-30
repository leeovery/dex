"""Which instances a server process serves, and how ids name them.

The roster comes entirely from the command line: the engine stays unaware
of which instances exist, so the arguments *are* the registry. An
instance's name is its directory basename, and every id the server hands
out is namespaced ``<instance>/<item-id>`` — which is what keeps instance
boundaries at the content level while one process reads several trees. An
id therefore parses back to the instance that owns it, and two roots
sharing a basename are refused at startup rather than making every id
ambiguous.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dex_engine.pipeline.types import Instance

__all__ = ["NotFoundError", "Roster", "build_roster", "qualify"]

# The two directories every read goes through. Enough to tell an instance
# root from a directory someone pointed at by mistake, and no more: `wiki/`
# and `enrichment/` are legitimately absent from a young instance.
_INSTANCE_DIRS = ("corpus", "state")


class NotFoundError(ValueError):
    """Nothing here answers to what was asked for.

    An unknown instance name, an id that is not two parts, an item no
    corpus holds, a wiki page name with no page. The MCP boundary restates
    it as the tool error the caller reads; anything else escaping a tool is
    a crash whose text the caller never sees, so every refusal a caller can
    act on has to arrive as this — or as another :class:`ValueError`, which
    the boundary treats the same way.
    """


def qualify(instance: str, item_id: str) -> str:
    """The namespaced id for one item — THE id format every result states."""
    return f"{instance}/{item_id}"


@dataclass(frozen=True, slots=True)
class Roster:
    """The served instances, keyed by name, in the order they were named."""

    instances: Mapping[str, Instance]

    def select(self, name: str | None) -> list[tuple[str, Instance]]:
        """The instances one call addresses: all of them, or just the one named.

        Args:
            name: An instance name, or ``None`` to fan out across the roster.

        Returns:
            ``(name, instance)`` pairs in roster order.

        Raises:
            NotFoundError: ``name`` is not a served instance.
        """
        return list(self.instances.items()) if name is None else [(name, self.locate(name))]

    def locate(self, name: str) -> Instance:
        """The instance served under ``name``.

        Raises:
            NotFoundError: No instance is served under that name; the message
                lists the ones that are.
        """
        instance = self.instances.get(name)
        if instance is None:
            served = ", ".join(self.instances) or "nothing"
            raise NotFoundError(f"no instance named {name!r} — this server serves: {served}")
        return instance

    def resolve(self, item_id: str) -> tuple[str, Instance, str]:
        """Parse a namespaced id back to the instance that owns it.

        Args:
            item_id: ``<instance>/<item-id>``, as every result states it.

        Returns:
            The instance name, the instance, and the bare item id.

        Raises:
            NotFoundError: The id is not two non-empty parts, or names no served
                instance. A bare id names one file inside its instance and
                never a path, so a second separator is refused here rather
                than reaching the filesystem.
        """
        name, separator, bare = item_id.partition("/")
        if not separator or not name or not bare or "/" in bare:
            raise NotFoundError(
                f"{item_id!r} is not a dex item id — every id a search returns is "
                "'<instance>/<item-id>'"
            )
        return name, self.locate(name), bare


def build_roster(paths: Sequence[Path]) -> Roster:
    """Build the roster from the instance roots the command line named.

    Args:
        paths: The instance roots, in the order given.

    Returns:
        The roster, keyed by directory basename.

    Raises:
        ValueError: A path is not an existing instance root, or two of them
            share a basename.
    """
    instances: dict[str, Instance] = {}
    for path in paths:
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"{path}: no such directory")
        missing = [f"{name}/" for name in _INSTANCE_DIRS if not (root / name).is_dir()]
        if missing:
            raise ValueError(f"{path}: not a dex instance — it has no {' or '.join(missing)}")
        if root.name in instances:
            raise ValueError(
                f"{path}: two instances would answer to the name {root.name!r} — the "
                "name is the id namespace, so it has to be unique"
            )
        instances[root.name] = Instance(root=root)
    return Roster(instances)
