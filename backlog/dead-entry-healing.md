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

A dead owner also blocks a second item (#192), and that part is engine
work. A URL is enriched under one item only, so `enrich fetch` from
another item refuses it as "already enriches under item X" — even when
X's unit has been `dead` since an early engine and nothing of it is on
disk. The refusal is false, and the live content is unreachable from the
item that wants it until someone thinks to fetch it on X's own URL, which
lands it. Seen in dex-engineering on 2026-09-27: two GitHub blobs dead
since engine 0.0.1, both answering today; the same instance holds seven
more dead private-repo blob units from 0.0.1 whose files exist, most
likely condemned before fetches were authenticated. Picking this up means
deciding what a second item's fetch does when the owner holds only a dead
verdict (requeue the owner's unit in place is the obvious candidate),
alongside reseeding the early engine's dead verdicts generically.
