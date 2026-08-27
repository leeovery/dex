# The mutation audit cannot see run.py's methods

`./mutate run "dex_engine.pipeline.run.*"` generates mutants for the
module's top-level functions only: of the ~1000 mutants mutmut produces
for `run.py`, none target a method of `_Drain` or the other classes. This
is not a general class-method limitation — `drivers/instagram.py`'s
methods are instrumented normally (`xǁInstagramDriverǁ…` mutants) — so
something about `run.py` specifically defeats the instrumenter, and the
cause has not been isolated.

The consequence: the orchestrator's drain logic — `_download_media`,
`_apply`, the write paths, the redrain — is exactly the code the
close-of-feature audit is supposed to exercise, and for that file the
audit silently reports only on the module-level helpers. A green audit of
`dex_engine.pipeline.run.*` claims less than it appears to.

Found while auditing the media-stage byte-sniffing fix, which is why that
change keeps its decision logic in `detect.py`, where the audit reaches.

Picking this up means: isolate what stops mutmut instrumenting `run.py`'s
classes (size? the dataclass decorators? a parse failure it swallows?),
fix or report upstream, and until then treat a run.py audit's silence
about methods as a known blind spot rather than a pass.
