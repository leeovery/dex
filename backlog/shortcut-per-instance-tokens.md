# Per-instance tokens in the phone shortcut

The Send To Dex shortcut
has one token field shared by every dictionary entry, so that single PAT
must have access to every repo the dictionary names. Fine-grained tokens
span one account or organization at a time, so an instance set crossing
orgs (leeovery/* plus curated-retail/dex-curated) cannot share one
fine-grained token today. Shortcut-internal only — the engine and skills
authenticate through gh on the machine. To design: the dictionary entry
grows a per-instance token (e.g. value becomes `owner/repo|token`, or
each entry becomes a nested dictionary), the request step reads the
matched entry's own token, and the import questions ask per instance.
Requested 2026-08-24.
