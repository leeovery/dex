---
name: dex-lint
description: Health-check and repair this dex instance. Use when the owner asks for a health check, a lint, to "look it over", or periodically after a run of ingests.
---

# Health check (lint)

1. `git pull` — captures and other machines' commits arrive first.
2. `bin/dex sync` — refresh engine-managed machinery (skills, shim,
   .gitattributes); commit if anything changed.
3. `bin/dex inbox` — reconcile staged captures. It needs GitHub auth (gh
   logged in, or GITHUB_TOKEN); if it reports missing auth or any FAIL line,
   stop and fix that with the owner before continuing. If it materialized
   anything, commit and push immediately — the remote asset is already
   deleted, so the repo copy is now the only copy. If it reports orphaned
   assets, re-run after a minute before raising them with the owner (a
   capture may still be in flight).
4. Run `bin/dex lint` from the instance root — the mechanical report.
5. Fix, in order:
   - **Broken wikilinks** — typo → correct it; genuinely missing target → create
     the page if its members justify one, otherwise de-link.
   - **Bad citations** (id not in corpus) — find the right id or remove the claim.
   - **Orphans** (uncited, unledgered items) — read the digest: cite it on the
     best existing page, or ledger it into `uncategorized-shares` in
     `state/taxonomy.json` if genuinely low-signal.
   - **Index drift** — regenerate the affected `wiki/index.md` entries.
   - **Stale pages** (members newer than the page) — fold the newer items in via
     rewrite-not-append; if the new material supersedes old claims, move them to
     the history section marked superseded, never silently delete.
6. Judgment sweep over every page you touched: cross-page contradictions
   (surface with dates), self-citation, scope creep.
7. Re-apply `wiki/pins.md` (the owner's hand-corrections) after any regeneration —
   pins always win.
8. Append a `wiki/log.md` line; commit and push. Report findings and fixes to
   the owner in plain words — no jargon.
