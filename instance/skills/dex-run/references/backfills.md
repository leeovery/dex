# Backfills (exports in raw/)

Getting exports: Discord via
[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter), as
JSON — the format `bin/dex normalize` reads. Other sources: convert to that
shape, or feed items through capture instead.

A converted export must give every message `id`, `type` (`Default` or
`Reply`), `timestamp` as ISO 8601, and an `author` carrying `id` plus
`name` or `nickname`; any attachment needs `url` and `fileName`. A message
missing one is skipped rather than fatal, and a whole channel whose file
cannot be read is named and skipped while the rest normalize — so read the
whole normalize summary before treating a backfill as complete: the
`warn:` lines, any `export unreadable — skipped` channel, and any
`channel incomplete` line. A silently short cohort is the failure this
reports.

`bin/dex normalize` → scope-filter pass (judgment; purge via `bin/dex
exclude <file.json>`) → `bin/dex enrich run` → the per-item work at scale.
Per-driver politeness sleeps make large cohorts slow by design, and the
pacing is automatic: fresh work drains first, then rerun cohorts
(migration reseeds and other requeues) at most 50 per run — the report
states the cohort position, and the remainder drains itself across
subsequent runs. No `--limit` babysitting needed. For 500+ items: work in
waves; write work manifests BEFORE dispatching any parallel agents; every
agent contract includes "if an input is missing, STOP — do not improvise";
verify coverage mechanically between waves.
