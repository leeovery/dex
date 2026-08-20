"""Source drivers (§2): one per source shape, resolved via the ordered registry.

Dependency direction (§14): only ``pipeline/registry.py`` and
``pipeline/run.py`` import from this package. Drivers import pipeline
vocabulary (types, classify, urls) — never the ledger, never the corpus:
a driver returns a :class:`~dex_engine.pipeline.types.Result` and nothing
else.
"""
