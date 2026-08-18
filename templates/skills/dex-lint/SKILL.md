---
name: dex-lint
description: Health-check and repair this dex instance. Use when the owner asks for a health check, a lint, to "look it over", or periodically after a run of ingests.
---

# Health check (lint)

1. `bin/kb sync` — refresh engine-managed machinery (skills, shim, inbox
   workflow); commit if anything changed.
2. Run `bin/kb lint` from the instance root — the mechanical report.
3. Fix, in order:
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
4. Judgment sweep over every page you touched: cross-page contradictions
   (surface with dates), self-citation, scope creep.
5. Re-apply `wiki/pins.md` (the owner's hand-corrections) after any regeneration —
   pins always win.
6. Append a `wiki/log.md` line; commit. Report findings and fixes to the owner in
   plain words — no jargon.
