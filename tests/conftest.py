"""Shared fixtures for the engine test suite."""

from pathlib import Path

import pytest

from dex_engine.pipeline.types import Instance


@pytest.fixture
def instance(tmp_path: Path) -> Instance:
    """A skeleton instance: the corpus/state/enrichment/cache tree in a tmp dir."""
    inst = Instance(root=tmp_path)
    for directory in (inst.corpus_dir, inst.state_dir, inst.enrichment_dir, inst.cache_dir):
        directory.mkdir()
    return inst
