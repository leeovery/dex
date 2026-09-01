"""Migration 10 — compile the map artifacts existing instances never had.

``state/map.json`` and the rendered ``wiki/index.md`` are derived state,
kept fresh by the verbs that rewrite their inputs and by the run flow's
own compile step — triggers that fire only on an instance already past
this release. Without a catch-up, an existing instance would answer the
serve map tools with "no compiled map" until its next run, and its
model-written index would stand, drift and all, until a session happened
to replace it. So the compile runs once here, at sync time: both
artifacts exist the moment the instance lands on this release, and the
rendered catalog takes the index over.

The compile is the engine's own — the same read every trigger and lint's
freshness diff use — so what lands here is byte-identical to what the
next trigger would write. Both artifacts derive whole from current
inputs and carry no judgment of their own, which is the idempotency: a
write happens only where the artifact differs from the compile, so a
second apply writes nothing, and a log-race re-application recompiles
what the triggers already keep fresh and destroys nothing.

A missing taxonomy or entity-members file is a young instance and
compiles honestly empty artifacts. A malformed one refuses the compile
with nothing written — and the refusal lands as an anomaly, never a
raised sync failure: the repair is session judgment on the named state
file, and a migration that raised instead would stop every future sync
at step 0 of every run, ahead of the very machinery refresh that ships
the repair procedure. Lint flags the absent artifacts until `bin/dex
map` runs after the repair.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic, instance_map
from dex_engine.pipeline.types import Instance, MigrationReport

__all__ = ["MapArtifactsCompile", "build"]

NUMBER = 10
INTENT = (
    "compile the derived map artifacts at sync time: state/map.json and the rendered "
    "wiki/index.md exist before the instance's next run, so the serve map tools answer "
    "at once and the rendered catalog replaces the model-written index"
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "MapArtifactsCompile":
    """Build migration 10 (the shared build signature; no state is stamped)."""
    return MapArtifactsCompile()


class MapArtifactsCompile:
    """Migration 10: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Compile the map and the index from current inputs; write what differs.

        Args:
            root: The instance root.

        Returns:
            The report: one action naming the artifact(s) written with the
            compile's counts, empty when both already match the compile,
            or one anomaly when a malformed judgment file refused it.
        """
        instance = Instance(root=root)
        try:
            compiled = instance_map.compile_map(instance)
        except ValueError as e:
            return MigrationReport(
                anomalies=[
                    (
                        f"the map compile refused: {e} — nothing written; repair the named "
                        "state file (session judgment), then `bin/dex map` builds "
                        "state/map.json and wiki/index.md (lint flags them until it runs)"
                    )
                ]
            )
        written: list[str] = []
        artifacts = (
            (instance.map_path, instance_map.serialize_map(compiled.payload)),
            (instance.index_path, compiled.index),
        )
        for path, rendered in artifacts:
            if path.exists() and path.read_bytes() == rendered.encode("utf-8"):
                continue
            path.parent.mkdir(exist_ok=True)
            atomic.write_text(path, rendered)
            written.append(str(path.relative_to(root)))
        if not written:
            return MigrationReport()
        return MigrationReport(
            actions=[
                (
                    f"compiled {' + '.join(written)}: {compiled.topics} topics, "
                    f"{compiled.entities} entities, {compiled.edges} edges"
                )
            ]
        )
