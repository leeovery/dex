# dex-engine

The shared pipeline engine for **dex** knowledge-base volumes. One brand, N brains:
each volume is a near-pure-content repo (corpus/enrichment/wiki/state) that runs
this engine as a dependency — no vendored copies, no per-volume maintenance.

Volumes: `dex-engineering` (the reference implementation), `dex-marketing`,
`dex-curated`, ...

## Running (from a volume's repo root)

```bash
uvx --from git+https://github.com/leeovery/dex-engine kb-normalize
uvx --from git+https://github.com/leeovery/dex-engine kb-enrich run
uvx --from git+https://github.com/leeovery/dex-engine kb-lint
uvx --from git+https://github.com/leeovery/dex-engine kb-exclude exclusions.json
```

Volumes typically carry a `bin/kb` shim wrapping the above. All commands resolve
paths from the current working directory — always run from the volume root.

## What a volume contains

```
CLAUDE.md      scope + operations (skeleton in templates/CLAUDE.md)
corpus/        provenance-stamped items (append-only)
enrichment/    fetched content per item
wiki/          generated pages (topics/entities/syntheses + index + log + pins)
state/         digests, ledgers, taxonomy, inbox
raw/           verbatim source exports, where applicable
.github/workflows/inbox.yml   thin caller -> inbox-reusable.yml here
```

## Capture

Any HTTP client (iOS Shortcut, script) opens a GitHub issue on the volume:
`title` = URL, `body` = optional note. The reusable inbox workflow appends it to
`state/inbox.md` and closes the issue. See `templates/shortcut.md`.

## Design

Karpathy llm-wiki pattern: raw sources are immutable; the wiki is a regenerable
build artifact; every wiki claim cites corpus item ids; trust is derived from
date + source; recency is first-class. Item schema: `docs/schema.md`.
