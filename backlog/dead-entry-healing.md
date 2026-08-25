# Pre-rewrite `dead` ledger entries need healing, per instance

The old
enricher could not tell a blocked fetch from a gone one, so transient
blocks were ledgered `dead`, a terminal status that is never retried. The
engine no longer does this (403/429/5xx → `blocked`, retried every run;
`dead` reserved for 404/410/NXDOMAIN), but migration 2 seeds only `done`
entries — verdicts the old code condemned are not reseeded. Known case: a
business site in one production instance (2026-08-19), hand-healed into
`enrichment/` while the ledger still says `dead`, so healed state and
ledger disagree. Close these out with `bin/dex enrich mark` during each
instance's post-merge sync review; it is per-instance content work, not
engine work.
