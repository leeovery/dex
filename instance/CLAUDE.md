# dex instance

A personal, LLM-maintained knowledge base. You (Claude) ARE the application:
you operate this repo per its contract (operations, dataflow, invariants,
conventions), which is engine-synced:

@.claude/dex-contract.md

This instance's lens, the owner's statement of what it reads for:

@lens.md

This file is engine-owned: `bin/dex sync` overwrites it, so never write
into it. What is specific to this instance lives in `lens.md`, which is
the owner's, and in `state/config.json`.
