# <volume> — <Domain> Knowledge Base (a dex volume)

A personal, LLM-maintained knowledge base. Engine: github.com/leeovery/dex-engine
(run via `bin/kb <cmd>` from the repo root). Claude operating over these files IS
the application.

## Scope

- **In**: <define>
- **Out**: <define>

## Operations

- **ingest** — new corpus items (inbox, exporters, manual drops): enrich, digest,
  update affected wiki pages + index, append to wiki/log.md.
- **query** — answer from wiki (index first, follow links); file real syntheses
  back under wiki/syntheses/.
- **lint** — `bin/kb lint` (mechanical) + agent judgment pass.

## Invariants

See engine README: corpus append-only; wiki regenerable, cites corpus ids only;
trust derived from date+source; recency first-class; provenance captured at
ingest.
